#!/usr/bin/env python3
"""\
Convert a Verilog simulation model to a VPR `pb_type.xml`

The following are allowed on a top level module:

    - `(* blackbox *)` : specify that the module has no interconnect or child
    pb_types (but if modes are used then its modes are allowed to have these).
    This will also set the BLIF model to be `.subckt <name>` unless CLASS is
    also specified.

    - `(* CLASS="input|output|lut|routing|mux|flipflop|mem" *)` : specify
    the class of an given instance.

    - `(* MODES="mode1; mode2; ..." *)` : specify that the module has more
    than one functional mode, each with a given name. The module will be
    evaluated n times, each time setting the MODE parameter to the nth value
    in the list of mode names. Each evaluation will be put in a pb_type
    `<mode>` section named accordingly.

    - `(* MODEL_NAME="model" *)` : override the name used for <model> and for
    ".subckt name" in the BLIF model. Mostly intended for use with w.py, when
    several different pb_types implement the same model.

The following are allowed on nets within modules
(TODO: use proper Verilog timing):
    - `(* SETUP="clk 10e-12" *)` : specify setup time for a given clock

    - `(* HOLD="clk 10e-12" *)` : specify hold time for a given clock

    - `(* CLK_TO_Q="clk 10e-12" *)` : specify clock-to-output time for
                                      a given clock

    - `(* DELAY_CONST_{input}="30e-12" *)` : specify a constant max delay
                                             from an input (applied to the
                                             output)

    - `(* DELAY_MATRIX_{input}="30e-12 35e-12; 20e-12 25e-12; ..." *)` :
        specify a VPR delay matrix (semicolons indicate rows). In this
        format columns specify inputs bits and rows specify output bits.
        This should be applied to the output.

    - `(* carry = ADDER *)` : specify carry chain pack_pattern associated
                              with this wire

    - `(* PACK = "<name1>[;<name2>[..]]" *)` : List of semicolon-separated
                                               pack pattern names to be used
                                               for a wire.

The following are allowed on ports:
    - `(* CLOCK *)` or `(* CLOCK=1 *)` : force a given port to be a clock

    - `(* CLOCK=0 *)` : force a given port not to be a clock

    - `(* ASSOC_CLOCK="RDCLK" *)` : force a port's associated clock to a
                                    given value

    - `(* PORT_CLASS="clock" *)` : specify the VPR "port_class"

    - `(* carry = ADDER *)` : specify carry chain pack_pattern associated
                              with this port

The following attributes are used to annotate cells with fasm metadata:
    - `(* FASM_PREFIX="prefix" *)` : Sets the fasm prefix of a cell instance.
        Cannot be set on a module definition!

    - `(* FASM_PREFIX="prefix1;prefix2;..." *) : Same as FASM_PREFIX but used
        for cells inside `generate` statements. Each instance of a cell will be
        assigned one prefix from the semicolon separated list. The number of
        prefixes in the list must match the cell instance count!

    - `(* FASM_FEATURES="str1;str2;..." *)` : Specifies a list of fasm features
        for the given module. Has to be provided at a module definition.
        Multiple features have to be separated by semicolon.

    - `*( FASM_FEATURES_{mode}="str1;str2;..." *)` : Same as above but defines
        a list of fasm features only for the given mode. Those will be attached
        to the pb_type corresponding to that mode.

    - `(* FASM_PARAMS="str1=param1;str2=param2;.." *)` : A semicolon separated
        list of fasm features and module parameters that they are to be
        assigned with. Can only be specified for a module definition.

The Verilog define "PB_TYPE" is set during generation.
"""

import os
import sys
import re

from typing import List, Dict, Tuple
from collections import defaultdict

import lxml.etree as ET

from .yosys import run
from .yosys.json import YosysJSON
from .yosys import utils as utils

from .xmlinc import xmlinc  # noqa: E402


def normalize_pb_name(pb_name):
    """ Some pb_type names generatedby the tool
        are illegal in VPR. This function converts them to
        legal ones e.g:

        output_dffs_gen[0].q_out_ff -> output_dffs_gen_q_out_ff_0
    """
    if pb_name is None:
        return None

    index = re.search(r'\[[0-9]+\](?!$)', pb_name)
    normalized_name = pb_name.replace('.', '_')
    if index is not None:
        normalized_name = normalized_name.replace(
            index.group(0), ""
        ) + index.group(0).replace('[', '_').replace(']', '')

    return normalized_name


def is_mod_blackbox(mod):
    """ Returns true if module is annotated with blackbox (or equivalent).

    Yosys supports 3 attributes that denote blackbox behavior:

    "blackbox" - Blackbox with no internal wiring
    "whitebox" - Blackbox with internal connections and timing.
    "lib_whitebox" - Like "whitebox" when read with "-lib", otherwise
        attribute is removed.

    """

    return (mod.attr("lib_whitebox", 0) == 1) or \
           (mod.attr("whitebox", 0) == 1) or \
           (mod.attr("blackbox", 0) == 1)


# $genblock$/vlog/tests/multiple_instance/multiple_instance.sim.v:12$64[57].\comb
GENBLOCK_REGEX = re.compile(
    "^\\$genblock\\$.*:[0-9]+\\$[0-9]+\\[(.*)\\]\\.\\\\(.*)"
)


def strip_name(name: str, include_index=True) -> str:
    """Convert generate block into normal array form.

    >>> n = r"$genblock$/tests/multiple_instance/multiple_instance.sim.v:12$64[57].\\comb" # noqa: E501
    >>> strip_name(n)
    'comb[57]'
    >>> strip_name(n, False)
    'comb'
    >>> n = r"$genblock$/tests/multiple_instance/multiple_instance.sim.v:15$10[3].\\comba" # noqa: #501
    >>> strip_name(n)
    'comba[3]'
    """
    if name.startswith('$genblock$'):
        index, name = GENBLOCK_REGEX.match(name).groups()
        if include_index:
            return "{}[{}]".format(name, index)
        else:
            return name
    return name


CellName = str
CellType = str
PinName = str
CellPin = Tuple[CellName, PinName]


def parse_fasm_attribute(attribute):
    """
    Parses a FASM attribute name and returns:
    - (None, None) if it is not a FASM attribute
    - (name, None) for FASM attribute not related to any mode
    - (name, mode name) for FASM attribute which is related to a mode
    """

    KNOWN_FASM_ATTRS = (
        "FASM_PREFIX", "FASM_FEATURES", "FASM_PARAMS",
    )

    # This attribute is not mode related
    if attribute in KNOWN_FASM_ATTRS:
        return attribute, None

    # Build a regex
    regex = "^({})_([A-Z0-9_]+)".format("|".join(KNOWN_FASM_ATTRS))

    # Try matching
    match = re.match(regex, attribute)
    if not match:
        return None, None

    # Extract metadata name and mode name
    return match.group(1), match.group(2)


def metadata_from_attributes(attributes, mode=None):
    """
    Collects metadata and stores it as a dict
    """

    metadata = {}

    # Process attributes
    for attr, value in attributes.items():

        # Parse attribute name
        attr_name, attr_mode = parse_fasm_attribute(attr)
        if attr_name is None:
            continue

        # Filter out if not for this mode
        if mode is not None and attr_mode != mode:
            continue

        # Store
        metadata[attr_name.lower()] = value

    return metadata


def metadata_from_incl_xml(incl_xml):
    """
    Retrieves metadata from the XML <metadata> tag and stores it in a dict
    """

    if incl_xml is None:
        return dict()

    return {tag.get("name"): tag.text for tag in incl_xml.findall("meta")}


def metadata_to_xml(xml_parent, metadata):
    """
    Formats the XML metadata tag using data from a dict
    """

    if len(metadata) == 0:
        return None

    metadata_xml = ET.SubElement(xml_parent, "metadata")
    for key, value in metadata.items():
        value = str(value)

        # Replace semicolons with newlines if any.
        if ";" in value:
            value = value.replace(";", "\n")

        meta_xml = ET.SubElement(metadata_xml, "meta", name=key)
        meta_xml.text = value

    return metadata_xml


def update_metadata(metadata, new_metadata, concatenate_keys=()):
    """
    Updates one dictionary with elements from another.
    """

    for key, value in new_metadata.items():

        # New key
        if key not in metadata:
            metadata[key] = value

        # Existing key
        else:

            # Concatenate
            if key in concatenate_keys:

                # When concatenating non-strings convert the result to string
                if not isinstance(metadata[key], str):
                    metadata[key] = str(metadata[key])

                metadata[key] += " " + str(value)

            # Do not merge, throw an error
            elif metadata[key] != value:
                print("ERROR: metadata conflict (!): '{}'='{}' vs '{}'"
                      .format(key, metadata[key], value))
                exit(-1)

    return metadata


def sanity_check_child_metadata(metadata):
    """
    Checks if metadata specified on a child pb_type is sane.
    """

    not_allowed_keys = ("fasm_params", "fasm_lut", )
    for key in metadata.keys():
        if key in not_allowed_keys:
            print("ERROR: metadata '{}' is not allowed for module instances"
                  .format(key))
            exit(-1)


def sanity_check_parent_metadata(metadata):
    """
    Checks if metadata specified on a parent pb_type is sane.
    """

    not_allowed_keys = ("fasm_prefix", )
    for key in metadata.keys():
        if key in not_allowed_keys:
            print("ERROR: metadata '{}' is not allowed for module definition"
                  .format(key))
            exit(-1)


def get_lut_bits(module):
    """
    Returns number of LUT address bits given a LUT module
    """

    # Find the "lut_in" port
    for port in module.ports:
        port_name = port[0]
        port_dir = port[3]
        if port_name == "lut_in" and port_dir == "input":
            port_width = port[1]
            return int(port_width)

    return None


def create_port(
        dir_xml: ET.Element, cell_pin: CellPin, direction: str, metadata=None
) -> ET.Element:
    cell_name, pin_name = cell_pin
    port = dict()
    port['name'] = pin_name
    port['type'] = direction
    if cell_name:
        port['from'] = cell_name
    port_xml = ET.SubElement(dir_xml, 'port', port)

    if metadata:
        meta_root = ET.SubElement(port_xml, "metadata")
        for name, value in metadata.items():
            meta_type = ET.SubElement(meta_root, 'meta', {'name': name})
            meta_type.text = value

    return port_xml


def copy_attrs(dst, srcs):
    # Find attributes which are on all srcs dictionaries
    all_have = []
    for attr in set(sum((list(s.keys()) for s in srcs), [])):
        if len(srcs) != sum(bool(attr in s) for s in srcs):
            continue
        all_have.append(attr)

    for attr in all_have:
        avalue = srcs[0][attr]
        avalues = [s[attr] for s in srcs[1:]]

        for other_avalue in avalues:
            if avalue == other_avalue:
                continue
            raise ValueError('{} values: {}'.format(attr, [avalue] + avalues))

        if attr in dst:
            if avalue != dst[attr]:
                raise ValueError(
                    '{} on net has value {} but pins have {}'.format(
                        attr, dst[attr], avalue
                    )
                )
        dst[attr] = avalue


def net_and_pin_attrs(yj, mod, driver: CellPin, sink: CellPin, netid: int):
    def filter_src(x):
        d = {}
        for k, v in x.items():
            if k == 'src':
                continue
            d[k] = v
        return d

    potential_attrs = []

    driver_cell, driver_pin = driver
    driver_type = mod.cell_type(driver_cell)
    if driver_type is not None:
        dmod = yj.module(driver_type)
        potential_attrs.append(filter_src(dmod.port_attrs(driver_pin)))

    sink_cell, sink_pin = sink
    sink_type = mod.cell_type(sink_cell)
    if sink_type is not None:
        smod = yj.module(sink_type)
        potential_attrs.append(filter_src(smod.port_attrs(sink_pin)))

    net_attrs = filter_src(mod.net_attrs_by_netid(netid))
    copy_attrs(net_attrs, potential_attrs)
    return net_attrs


def make_direct_conn(
        ic_xml: ET.Element, driver: CellPin, sink: CellPin, path_attr: dict
) -> ET.Element:
    dir_xml = ET.SubElement(ic_xml, 'direct')
    create_port(dir_xml, driver, "input")
    create_port(dir_xml, sink, "output")

    # Pack patterns
    pack = path_attr.get('pack', path_attr.get('PACK', None))
    if pack is not None:
        for pack_name in pack.split(";"):
            pp_xml = ET.SubElement(
                dir_xml, 'pack_pattern', {
                    'name': pack_name,
                    'type': 'pack'
                }
            )
            create_port(pp_xml, driver, "input")
            create_port(pp_xml, sink, "output")

    carry_name = path_attr.get('carry', None)
    if carry_name:
        pp_xml = ET.SubElement(
            dir_xml, 'pack_pattern', {
                'name': carry_name,
                'type': 'carry'
            }
        )
        create_port(pp_xml, driver, "input")
        create_port(pp_xml, sink, "output")

    return dir_xml


def make_mux_conn(
        ic_xml: ET.Element, mux_name: str, mux_inputs: Dict[CellPin, CellPin],
        mux_outputs: Dict[CellPin, List[CellPin]]
) -> ET.Element:

    mux_xml = ET.SubElement(ic_xml, "mux", {"name": mux_name})
    for mux_input, driver in mux_inputs.items():
        create_port(mux_xml, driver, "input", metadata={'fasm_mux': mux_input})
    assert len(mux_outputs) == 1, mux_outputs
    for mux_pin, sinks in mux_outputs.items():
        assert len(sinks) == 1, sinks
        for sink_pin, path_attr in sinks:
            create_port(mux_xml, sink_pin, "output")

    #  <metadata>
    meta_root = ET.SubElement(mux_xml, 'metadata')
    #    <meta name="type">bel</meta>
    meta_type = ET.SubElement(meta_root, 'meta', {'name': 'type'})
    meta_type.text = "bel"
    #    <meta name="subtype">routing</meta>
    meta_subtype = ET.SubElement(meta_root, 'meta', {'name': 'subtype'})
    meta_subtype.text = "routing"

    return mux_xml


def get_interconnects(yj, mod, mod_pname: str,
                      valid_names) -> Dict[CellPin, List[CellPin]]:
    """Get the connectivity of module.

    Returns:
        A dictionary containing with a list of sink pins for each driver pin.
    """
    interconn = defaultdict(list)

    # Cell connections
    for cname, ctype in mod.cells:
        pb_name = strip_name(cname)
        assert pb_name in valid_names, (pb_name, valid_names)
        if pb_name == mod_pname:
            pb_name = None

        # All interconnect going INTO a cell (top level or children).
        inp_cons = mod.cell_conns(cname, "input")
        for pin, net in inp_cons:
            drvs = mod.net_drivers(net)
            assert len(drvs) > 0, (
                "ERROR: pin {}.{} has no driver, \
                interconnect will be missing\n{}"
                .format(cname, pin, mod)
            )
            assert len(drvs) < 2, (
                "ERROR: pin {}.{} has multiple drivers, \
                interconnect will be overspecified"
                .format(cname, pin)
            )
            for drv_cell, drv_pin in drvs:
                net_attr = net_and_pin_attrs(
                    yj, mod, (drv_cell, drv_pin), (pb_name, pin), net
                )

                drv_cell_name = strip_name(drv_cell)
                assert drv_cell_name in valid_names
                if drv_cell_name == mod_pname:
                    drv_cell_name = None
                interconn[(drv_cell_name,
                           drv_pin)].append(((pb_name, pin), net_attr))

        # Only consider outputs from cell to top level IO.
        # Inputs to other cells will be dealt with in those cells.
        out_cons = mod.cell_conns(cname, "output")
        for pin, net in out_cons:
            sinks = mod.net_sinks(net)
            for sink_cell, sink_pin in sinks:
                if sink_cell != mod.name:
                    continue
                net_attr = net_and_pin_attrs(
                    yj, mod, (pb_name, pin), (None, sink_pin), net
                )
                interconn[(pb_name, pin)].append(((None, sink_pin), net_attr))

    # Passthrough connections. Get ports along with connections
    inp_ports = [p for p in mod.ports if p[3] == "input"]
    out_ports = [p for p in mod.ports if p[3] == "output"]

    # Loop over outputs and assign them with connected inputs
    for out_port in out_ports:
        for out_bit, out_net in enumerate(out_port[2]):

            # Format full output port name
            if out_port[1] == 1:
                out_name = out_port[0]
            else:
                out_name = "{}[{}]".format(out_port[0], out_bit)

            # Find input
            for inp_port in inp_ports:
                for inp_bit, inp_net in enumerate(inp_port[2]):

                    # Format full input port name
                    if inp_port[1] == 1:
                        inp_name = inp_port[0]
                    else:
                        inp_name = "{}[{}]".format(inp_port[0], inp_bit)

                    # Find matching nets
                    if out_net == inp_net:
                        key = (None, inp_name)
                        val = ((None, out_name), {})
                        interconn[key].append(val)

    import pprint
    pprint.pprint(list(interconn.values()))

    def pin_sort(p):
        pin, attr = p
        if pin[0] is None:
            return ('', pin[1])
        else:
            return pin

    for l in interconn.values():
        l.sort(key=pin_sort)

    return interconn


def mode_interconnects(mod, mode_name) -> Dict[CellPin, List[CellPin]]:
    """
    This function returns a definition of an interconnect used to connect
    a child pb_type for the given mode with its parent pb_type that provides
    the modes.

    The returned dict is indexed by tuples containing source (driver) mode
    names and pin names. Its values contain lists of sink modes and pin names
    that are driven by the driver. If the mode name is None then the connection
    refers to the parent pb_type.
    """
    interconn = {}
    for name, width, bits, iodir in mod.ports:
        if iodir == "input":
            interconn[(None, name)] = [((mode_name, name,), {},)]
        else:
            interconn[(mode_name, name)] = [((None, name,), {},)]
    return interconn


CellPrefix = str
ChildrenDict = Dict[CellPrefix, Tuple[CellType, List[CellName]]]


def get_children(yj, mod) -> Tuple[ChildrenDict, ChildrenDict]:
    routing = dict()
    children = dict()
    for cname, ctype in mod.cells:
        cattrs = mod.cell_attrs(cname)

        # We currently special case routing muxes
        cell = yj.module(ctype)
        if cell.CLASS == "routing":
            d = routing
        else:
            d = children
        cname_prefix = strip_name(cname, False)
        if cname_prefix not in children:
            d[cname_prefix] = (ctype, [])
        assert d[cname_prefix][
            0
        ] == ctype, \
            "Type of {} with prefix {} doesn't match \
            existing. Type: {}, existing: {}".format(
                cname, cname_prefix, ctype, children[cname_prefix]
        )
        d[cname_prefix][-1].append((strip_name(cname), cattrs,))

    for d in (routing, children):
        for _, l in children.values():
            if len(l) > 1:
                l.sort(key=lambda t: t[0])
#                _, _ = get_list_name_and_length(l)

    return routing, children


def get_cellname_from_shortname(shortname, mod):
    for cname, ctype in mod.cells:
        if shortname != strip_name(cname):
            continue
        return cname
    raise NameError("No cell named {}".format(shortname))


def get_list_name_and_length(l: List[str]) -> Tuple[str, int]:
    """
    >>> get_list_name_and_length(['i[{}]'.format(i) for i in range(10)])
    ('i', 10)

    Assertion failure on missing value
    >>> get_list_name_and_length(['i[0]', 'i[2]'])
    Traceback (most recent call last):
        ...
    AssertionError: index 1 expected: i[1] != actual: i[2]
    ['i[0]', 'i[2]']

    Assertion failure when not starting at zero
    >>> get_list_name_and_length(['i[1]', 'i[2]'])
    Traceback (most recent call last):
        ...
    AssertionError: index 0 expected: i[0] != actual: i[1]
    ['i[1]', 'i[2]']

    Assertion failure when duplicate values

    >>> get_list_name_and_length(['i[0]', 'i[0]'])
    Traceback (most recent call last):
        ...
    AssertionError: index 1 expected: i[1] != actual: i[0]
    ['i[0]', 'i[0]']

    >>> get_list_name_and_length(['i[0]', 'i[1]', 'i[1]'])
    Traceback (most recent call last):
        ...
    AssertionError: index 2 expected: i[2] != actual: i[1]
    ['i[0]', 'i[1]', 'i[1]']

    >>> get_list_name_and_length(['i[0]', 'i[1]', 'i[1]', 'i[2]'])
    Traceback (most recent call last):
        ...
    AssertionError: index 2 expected: i[2] != actual: i[1]
    ['i[0]', 'i[1]', 'i[1]', 'i[2]']

    Assertion failure on incorrect formatting
    >>> get_list_name_and_length(['i{}'.format(i) for i in range(4)])
    Traceback (most recent call last):
        ...
    AssertionError: No index brackets found in item 0: i0
    ['i0', 'i1', 'i2', 'i3']

    Allow square brackets in name
    >>> get_list_name_and_length(['i[1][{}]'.format(i) for i in range(4)])
    ('i[1]', 4)
    """
    if not l:
        return True

    assert '[' in l[0], "No index brackets found in item 0: {}\n{}".format(
        l[0], l
    )
    list_name = l[0][:l[0].rfind('[')]
    sl = sorted(l, key=len)
    for i in range(0, len(l)):
        expected_item = "{}[{}]".format(list_name, i)
        assert expected_item == sl[
            i], "index {} expected: {} != actual: {}\n{}".format(
                i, expected_item, sl[i], sl
        )
    return list_name, len(l)


def make_ports(clocks, mod, pb_type_xml, only_type=None):
    for name, width, bits, iodir in mod.ports:
        ioattrs = {"name": name, "num_pins": str(width)}
        pclass = mod.net_attr(name, "PORT_CLASS")
        if pclass is not None:
            ioattrs["port_class"] = pclass
        if name in clocks:
            if only_type and only_type != "clocks":
                continue
            port_xml = ET.SubElement(pb_type_xml, "clock", ioattrs)
        elif iodir == "input":
            if only_type and only_type != "inputs":
                continue
            port_xml = ET.SubElement(pb_type_xml, "input", ioattrs)
        elif iodir == "output":
            if only_type and only_type != "outputs":
                continue
            port_xml = ET.SubElement(pb_type_xml, "output", ioattrs)
        else:
            assert False, "bidirectional ports not supported in VPR pb_types"

        port_attrs = mod.port_attrs(name)

        carry_name = port_attrs.get('carry', None)
        if carry_name:
            ET.SubElement(
                port_xml, 'pack_pattern', {
                    'name': carry_name,
                    'type': 'carry'
                }
            )


def make_container_pb(
        outfile, yj, mod, mod_pname, pb_type_xml, routing, children
):
    # Containers have to include children
    # ------------------------------------------------------------
    for child_prefix, (child_type, children_data) in children.items():
        # Work out were the child pb_type file can be found
        module_file = yj.get_module_file(child_type)
        module_path = os.path.dirname(module_file)
        module_basename = os.path.basename(module_file)
        module_prefix = re.match(r"([A-Za-z0-9_]+)\.sim\.v",
                                 module_basename).groups()[0]

        pb_type_path = "{}/{}.pb_type.xml".format(module_path, module_prefix)

        include_as_is = True
        comment_str = ""
        # Read the top level properties of the pb_type
        with open(pb_type_path, 'r') as inc_xml:
            xml_inc = ET.fromstring(inc_xml.read().encode('utf-8'))
            inc_attrib = xml_inc.attrib
            normalized_name = normalize_pb_name(child_prefix)
            num_pb = str(len(children_data))
            if normalized_name != inc_attrib['name']:
                comment_str += "old_name {}".format(inc_attrib['name'])
                inc_attrib['name'] = normalize_pb_name(child_prefix)
                include_as_is = False
            if num_pb != inc_attrib['num_pb']:
                comment_str += " old_num_pb {}".format(inc_attrib['num_pb'])
                inc_attrib['num_pb'] = str(len(children_data))
                include_as_is = False
            inc_metadata_xml = xml_inc.find("metadata")

        xptr = None
        parent_xml = pb_type_xml
        if include_as_is is not True:
            xptr = "xpointer(pb_type/child::node()[local-name()!='metadata'])"
            parent_xml = ET.SubElement(pb_type_xml, 'pb_type', inc_attrib)
            parent_xml.append(ET.Comment(comment_str))

        xmlinc.include_xml(
            parent=parent_xml, href=pb_type_path, outfile=outfile, xptr=xptr
        )

        # Get metadata from the included XML
        incl_medatata = metadata_from_incl_xml(inc_metadata_xml)

        # We have only one child of that type
        if len(children_data) == 1:
            child_attrs = children_data[0][1]

            # Get & check
            attr_metadata = metadata_from_attributes(child_attrs, None)
            sanity_check_child_metadata(attr_metadata)

            # Merge
            metadata = update_metadata(attr_metadata, incl_medatata)

        # Multiple children of the same type (array)
        else:

            metadata = {}
            fasm_prefix_count = 0

            # Merge the metadata
            children_attrs = [d[1] for d in children_data]
            for i, child_attrs in enumerate(children_attrs):

                # Get & check
                attr_metadata = metadata_from_attributes(child_attrs, None)
                sanity_check_child_metadata(attr_metadata)

                # Check if the prefix count for the cell array equals its
                # count. Isolate fasm_prefix for the correct child index and
                # use it.
                if "fasm_prefix" in attr_metadata:
                    fasm_prefixes = attr_metadata["fasm_prefix"].split(";")
                    if len(fasm_prefixes) != len(children_data):
                        print("ERROR: Number of fasm prefixes for cell array "
                              "must match its count!")
                        exit(-1)
                    attr_metadata["fasm_prefix"] = fasm_prefixes[i]

                # Merge
                metadata = update_metadata(
                    metadata,
                    attr_metadata, ("fasm_prefix"))

                # Count occurrences of the "fasm_prefix" key
                fasm_prefix_count += 1

            # Error, not all of the child instances have the "fasm_prefix"
            if fasm_prefix_count > 0 and \
               fasm_prefix_count != len(children_data):
                print("ERROR: The FASM_PREFIX attribute must be set on "
                      "all instances of given module type")
                exit(-1)

            # Merge included metadata
            metadata = update_metadata(metadata, incl_medatata)

        # Write the metadata
        metadata_to_xml(parent_xml, metadata)

    # Contains need interconnect to their children
    # ------------------------------------------------------------
    # Work out valid names for cells to sanity check the interconnects.
    valid_names = [mod_pname]

    routing_cells = []
    for _, children_data in routing.values():
        routing_cells.extend([c[0] for c in children_data])
    valid_names.extend(routing_cells)

    for _, children_data in children.values():
        valid_names.extend([c[0] for c in children_data])

    # Extract the interconnect from the module
    interconn = get_interconnects(yj, mod, mod_pname, valid_names)
    import pprint
    print(mod_pname)
    print("--")
    pprint.pprint(interconn)
    print("--")
    print(routing_cells)
    pprint.pprint(routing)
    print("--")

    # Generate the actual interconnect
    ic_xml = ET.SubElement(pb_type_xml, "interconnect")
    for (driver_cell, driver_pin), sinks in interconn.items():
        if driver_cell in routing_cells:
            continue
        for (sink_cell, sink_pin), path_attr in sinks:
            if sink_cell in routing_cells:
                continue
            make_direct_conn(
                ic_xml, (normalize_pb_name(driver_cell), driver_pin),
                (normalize_pb_name(sink_cell), sink_pin), path_attr
            )

    # Generate the mux interconnects
    for mux_cell in routing_cells:
        mux_outputs = defaultdict(list)
        for (driver_cell, driver_pin), sinks in interconn.items():
            if driver_cell != mux_cell:
                continue
            mux_outputs[driver_pin].extend(sinks)

        assert len(mux_outputs) == 1, """\
Mux {} has multiple outputs ({})!
Currently muxes can only drive a single output.""".format(
            mux_cell, ", ".join(mux_outputs.keys())
        )
        for mux_output_pin, sinks in mux_outputs.items():
            assert len(sinks) == 1, """\
Mux {}.{} has multiple outputs ({})!
Currently muxes can only drive a single output.""".format(
                mux_cell, mux_output_pin,
                ", ".join("{}.{}".format(*pin) for pin, path_attr in sinks)
            )
            for (sink_cell, sink_pin), path_attr in sinks:
                assert sink_cell not in routing_cells, """\
Mux {}.{} is trying to drive mux input pin {}.{}""".format(
                    mux_cell, mux_output_pin, sink_cell, sink_pin
                )

        mux_inputs = {}
        for (driver_cell, driver_pin), sinks in interconn.items():
            for (sink_cell, mux_pin), path_attr in sinks:
                if sink_cell != mux_cell:
                    continue
                assert driver_cell not in routing_cells, \
                    "Mux {}.{} is trying to drive mux {}.{}".format(
                        driver_cell, driver_pin, mux_cell, sink_pin
                    )
                assert sink_pin not in mux_inputs, """\
Pin {}.{} is trying to drive mux pin {}.{} (already driving by {}.{})\
                 """.format(
                    driver_cell, driver_pin, mux_cell, mux_pin,
                    *mux_inputs[sink_pin]
                )
                mux_inputs[mux_pin] = (driver_cell, driver_pin)

        make_mux_conn(ic_xml, mux_cell, mux_inputs, mux_outputs)


def make_leaf_pb(outfile, yj, mod, mod_pname, pb_type_xml):

    # As leaf node with "blif_model" set is a site., need to generate timing
    # information.
    def process_clocked_tmg(tmgspec, port, iodir, xmltype, xml_parent):
        """Add a suitable timing spec if necessary to the pb_type"""
        if tmgspec is not None:
            splitspec = tmgspec.split(" ")
            assert len(
                splitspec
            ) == 2, 'bad timing specification "{}", must be of format \
            "clock value"'.format(tmgspec)

            attrs = {"port": port, "clock": splitspec[0]}
            if xmltype == "T_clock_to_Q":
                assert iodir == "output", \
                    "Only output ports can have T_clock_to_Q timing \
                    definition. Port {}, direction {}.".format(
                        port, iodir)
                attrs["max"] = splitspec[1]
            else:
                attrs["value"] = splitspec[1]
            ET.SubElement(xml_parent, xmltype, attrs)

    for name, width, bits, iodir in mod.ports:
        port = "{}".format(name)
        # Clocked timing
        Tsetup = mod.net_attr(name, "SETUP")
        Thold = mod.net_attr(name, "HOLD")
        Tctoq = mod.net_attr(name, "CLK_TO_Q")
        process_clocked_tmg(Tsetup, port, iodir, "T_setup", pb_type_xml)
        process_clocked_tmg(Thold, port, iodir, "T_hold", pb_type_xml)
        process_clocked_tmg(Tctoq, port, iodir, "T_clock_to_Q", pb_type_xml)

        # Combinational delays
        dly_prefix = "DELAY_CONST_"
        dly_mat_prefix = "DELAY_MATRIX_"
        for attr, atvalue in mod.net_attrs(name).items():
            if attr.startswith(dly_prefix):
                # Single, constant delays
                inp = attr[len(dly_prefix):]
                inport = "{}".format(inp)
                ET.SubElement(
                    pb_type_xml, "delay_constant", {
                        "in_port": inport,
                        "out_port": port,
                        "max": str(atvalue)
                    }
                )
            elif attr.startswith(dly_mat_prefix):
                # Constant delay matrices
                inp = attr[len(dly_mat_prefix):]
                inport = "{}".format(inp)
                mat = "\n" + atvalue.replace(";", "\n") + "\n"
                xml_mat = ET.SubElement(
                    pb_type_xml, "delay_matrix", {
                        "in_port": inport,
                        "out_port": port,
                        "type": "max"
                    }
                )
                xml_mat.text = mat


def make_pb_type(
        infiles, outfile, yj, mod, mode_processing=False,
        mode_xml=None, mode_name=None, select_prefix=""
):
    """Build the pb_type for a given module. mod is the YosysModule object to
    generate."""

    modes = mod.attr("MODES", None)
    if modes is not None:
        modes = modes.split(";")

    is_blackbox = mod.attr("blackbox", 0) != 0

    # Create metadata
    metadata = metadata_from_attributes(mod.module_attrs, mode_name)

    mod_pname = mod.name
    assert mod_pname == mod_pname.upper(
    ), "pb_type name should be all uppercase. {}".format(mod_pname)

    pb_attrs = dict()
    # If we are a blackbox with no modes, then generate a blif_model
    is_blackbox = is_mod_blackbox(mod) or not mod.cells
    has_modes = modes is not None

    print("is_blackbox", is_blackbox, "has_modes?", has_modes)

    # Process type and class of module
    model_name = mod.attr("MODEL_NAME", mod.name)
    mod_cls = mod.CLASS
    if mod_cls is not None:
        if mod_cls == "input":
            pb_attrs["blif_model"] = ".input"
        elif mod_cls == "output":
            pb_attrs["blif_model"] = ".output"
        elif mod_cls == "lut":
            pb_attrs["blif_model"] = ".names"
            pb_attrs["class"] = "lut"
        elif mod_cls == "routing":
            # TODO: pb_attrs["class"] = "routing"
            pass
        elif mod_cls == "mux":
            # TODO: ?
            pass
        elif mod_cls == "flipflop":
            pb_attrs["blif_model"] = ".latch"
            pb_attrs["class"] = "flipflop"
        else:
            assert False, "unknown class {}".format(mod_cls)
    elif is_blackbox and not has_modes:
        pb_attrs["blif_model"] = ".subckt " + model_name

    # set num_pb to 1, it will be updated if this pb_type
    # will be included by another one
    if mode_xml is None:
        pb_type_xml = ET.Element(
            "pb_type", {
                "num_pb": "1",
                "name": mod_pname
            },
            nsmap={'xi': xmlinc.xi_url}
        )
    else:
        pb_type_xml = ET.SubElement(
            mode_xml,
            "pb_type", {
                "num_pb": "1",
                "name": mode_name
            },
            nsmap={'xi': xmlinc.xi_url}
        )

    if 'blif_model' in pb_attrs:
        ET.SubElement(pb_type_xml, "blif_model",
                      {}).text = pb_attrs["blif_model"]

    if 'class' in pb_attrs:
        ET.SubElement(pb_type_xml, "pb_class", {}).text = pb_attrs["class"]

    # Create the pins for this pb_type
    clocks = set(run.list_clocks(infiles, mod.name, prefix=select_prefix))

    # Add extra clocks inferred from port names
    # Mask out clocks with the attribute "CLOCK" not equal to 1
    for name, width, bits, iodir in mod.ports:
        port_attrs = mod.port_attrs(name)

        is_clock = utils.is_clock_name(name)

        # In pb_type "clock" ports can be only inputs. Clock outputs must
        # be declared as "output".
        if iodir == "output":
            is_clock = False

        if "CLOCK" in port_attrs:
            is_clock = int(port_attrs["CLOCK"]) != 0

        if is_clock:
            clocks.add(name)
        else:
            clocks.discard(name)

    make_ports(clocks, mod, pb_type_xml, "clocks")
    make_ports(clocks, mod, pb_type_xml, "inputs")
    make_ports(clocks, mod, pb_type_xml, "outputs")

    if modes and not mode_processing:
        for mode in modes:
            smode = mode.strip()
            mode_xml = ET.SubElement(pb_type_xml, "mode", {"name": smode})
            # Rerun Yosys with mode parameter
            mode_yj = YosysJSON(
                run.vlog_to_json(
                    infiles,
                    flatten=False,
                    aig=False,
                    mode=smode,
                    module_with_mode=mod.name
                )
            )
            mode_mod = mode_yj.module(mod.name)

            inter = {}

            # The mode has no children. Don't generate a pb_type then. Make
            # only the interconnect instead.
            if len(mode_mod.cells) == 0:
                inter.update(get_interconnects(
                    mode_yj, mode_mod, smode, [smode]))

                # Check if we have metadata. Metadata cannot be appended to the
                # "mode" tag.
                mode_metadata = metadata_from_attributes(
                    mode_mod.module_attrs,
                    smode)

                if len(mode_metadata):
                    print("ERROR: Cannot assign metadata to a mode without "
                          "children cells")
                    exit(-1)

            # The mode has children, recurse
            else:
                make_pb_type(infiles, outfile, mode_yj, mode_mod,
                             True, mode_xml, smode, select_prefix)
                inter.update(mode_interconnects(mod, smode))

            # Add or update the interconnect.
            ic_xml = mode_xml.find("interconnect")
            if ic_xml is None:
                ic_xml = ET.SubElement(mode_xml, "interconnect")

            for (driv_cell, driv_pin), sinks in inter.items():
                for (sink_cell, sink_pin), attrs in sinks:
                    make_direct_conn(
                        ic_xml,
                        (driv_cell, driv_pin),
                        (sink_cell, sink_pin),
                        attrs)

    if not modes or mode_processing:
        routing = children = []
        if not is_blackbox:
            routing, children = get_children(yj, mod)

        if routing or children:
            make_container_pb(
                outfile, yj, mod, mod_pname, pb_type_xml, routing, children
            )
        else:
            make_leaf_pb(outfile, yj, mod, mod_pname, pb_type_xml)

    # Check the metadata
    sanity_check_parent_metadata(metadata)
    # Write it
    metadata_to_xml(pb_type_xml, metadata)

    return pb_type_xml


def vlog_to_pbtype(infiles, outfile, top=None):

    # Check if Yosys requires 'select' prefix
    select_prefix = run.determine_select_prefix()

    iname = os.path.basename(infiles[0])

    run.add_define("PB_TYPE")
    vjson = run.vlog_to_json(infiles, flatten=False, aig=False)
    yj = YosysJSON(vjson)

    if top is not None:
        top = top
    else:
        wm = re.match(r"([A-Za-z0-9_]+)\.sim\.v", iname)
        if wm:
            top = wm.group(1).upper()
        else:
            print(
                "ERROR file name not of format %.sim.v ({}),"
                " cannot detect top level. Manually specify"
                " the top level module using --top".
                format(iname)
            )
            sys.exit(1)

    top = top.upper()

    tmod = yj.module(top)

    pb_type_xml = make_pb_type(
        infiles, outfile, yj, tmod, select_prefix=select_prefix)

    return ET.tostring(
        pb_type_xml,
        pretty_print=True,
        encoding="utf-8",
        xml_declaration=True
    ).decode('utf-8')
