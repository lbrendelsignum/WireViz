#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import platform
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple, Union

import yaml

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent))  # add src/wireviz to PATH

from wireviz.data import Metadata, Options, Tweak
from wireviz.harness import Harness
from wireviz.helper import (
    expand,
    file_read_text,
    get_single_key_and_value,
    is_arrow,
    smart_file_resolve,
)

from . import APP_NAME


def parse(
    inp: Union[Path, str, Dict],
    return_types: Union[None, str, Tuple[str]] = None,
    output_formats: Union[None, str, Tuple[str]] = None,
    output_dir: Union[str, Path] = None,
    output_name: Union[None, str] = None,
    image_paths: Union[Path, str, List] = [],
) -> Any:
    """
    This function takes an input, parses it as a WireViz Harness file,
    and outputs the result as one or more files and/or as a function return value

    Accepted inputs:
        * A path to a YAML source file to parse
        * A string containing the YAML data to parse
        * A Python Dict containing the pre-parsed YAML data

    Supported return types:
        * "png":     the diagram as raw PNG data
        * "svg":     the diagram as raw SVG data
        * "harness": the diagram as a Harness Python object

    Supported output formats:
        * "csv":  the BOM, as a comma-separated text file
        * "gv":   the diagram, as a GraphViz source file
        * "html": the diagram and (depending on the template) the BOM, as a HTML file
        * "png":  the diagram, as a PNG raster image
        * "pdf":  the diagram and (depending on the template) the BOM, as a PDF file
        * "svg":  the diagram, as a SVG vector image
        * "tsv":  the BOM, as a tab-separated text file

    Args:
        inp (Path | str | Dict):
            The input to be parsed (see above for accepted inputs).
        return_types (optional):
            One of the supported return types (see above), or a tuple of multiple return types.
            If set to None, no output is returned by the function.
        output_formats (optional):
            One of the supported output types (see above), or a tuple of multiple output formats.
            If set to None, no files are generated.
        output_dir (Path | str, optional):
            The directory to place the generated output files.
            Defaults to inp's parent directory, or cwd if inp is not a path.
        output_name (str, optional):
            The name to use for the generated output files (without extension).
            Defaults to inp's file name (without extension).
            Required parameter if inp is not a path.
        image_paths (Path | str | List, optional):
            Paths to use when resolving any image paths included in the data.
            Note: If inp is a path to a YAML file,
            its parent directory will automatically be included in the list.

    Returns:
        Depending on the return_types parameter, may return:
        * None
        * one of the following, or a tuple containing two or more of the following:
            * PNG data
            * SVG data
            * a Harness object
    """

    if not output_formats and not return_types:
        raise Exception("No output formats or return types specified")

    yaml_data, yaml_file = _get_yaml_data_and_path(inp)
    if not isinstance(yaml_data, dict):
        raise TypeError(f"Expected a dict as top-level YAML input, but got: {type(yaml_data)}")
    if output_formats:
        # need to write data to file, determine output directory and filename
        output_dir = _get_output_dir(yaml_file, output_dir)
        output_name = _get_output_name(yaml_file, output_name)
        output_file = output_dir / output_name

    if yaml_file:
        # if reading from file, ensure that input file's parent directory is included in image_paths
        default_image_path = yaml_file.parent.resolve()
        if default_image_path not in [Path(x).resolve() for x in image_paths]:
            image_paths.append(default_image_path)

    # define variables =========================================================
    # containers for parsed component data and connection sets
    template_connectors = {}
    template_cables = {}
    connection_sets = []
    # actual harness
    harness = Harness(
        metadata=Metadata(**yaml_data.get("metadata", {})),
        options=Options(**yaml_data.get("options", {})),
        tweak=Tweak(**yaml_data.get("tweak", {})),
    )
    # others
    # store mapping of components to their respective template
    designators_and_templates = {}
    # keep track of auto-generated designators to avoid duplicates
    autogenerated_designators = {}

    # When title is not given, either deduce it from filename, or use default text.
    if "title" not in harness.metadata:
        harness.metadata["title"] = output_name or f"{APP_NAME} diagram and BOM"

    # add items
    # parse YAML input file ====================================================

    sections = ["connectors", "cables", "connections"]
    types: list[type] = [dict, dict, list]
    for sec, ty in zip(sections, types):
        if sec in yaml_data and type(yaml_data[sec]) is ty:  # section exists
            if len(yaml_data[sec]) > 0:  # section has contents
                if ty is dict:
                    for key, attribs in yaml_data[sec].items():
                        # The Image dataclass might need to open an image file with a relative path.
                        image = attribs.get("image")
                        if isinstance(image, dict):
                            image_path = image["src"]
                            if image_path and not Path(image_path).is_absolute():
                                # resolve relative image path
                                image["src"] = smart_file_resolve(image_path, image_paths)
                        if sec == "connectors":
                            template_connectors[key] = attribs
                        elif sec == "cables":
                            template_cables[key] = attribs
            else:  # section exists but is empty
                pass
        else:  # section does not exist, create empty section
            if ty is dict:
                yaml_data[sec] = {}
            elif ty is list:
                yaml_data[sec] = []

    connection_sets = yaml_data["connections"]

    # go through connection sets, generate and connect components ==============

    template_separator_char = harness.options.template_separator

    def resolve_designator(inp, separator):
        if separator in inp:  # generate a new instance of an item
            if inp.count(separator) > 1:
                raise Exception(f"{inp} - Found more than one separator ({separator})")
            template, designator = inp.split(separator)
            if designator == "":
                autogenerated_designators[template] = autogenerated_designators.get(template, 0) + 1
                designator = f"__{template}_{autogenerated_designators[template]}"
            # check if redefining existing component to different template
            if designator in designators_and_templates:
                if designators_and_templates[designator] != template:
                    raise Exception(
                        f"Trying to redefine {designator} from {designators_and_templates[designator]} to {template}"
                    )
            else:
                designators_and_templates[designator] = template
        else:
            template, designator = (inp, inp)
            if designator in designators_and_templates:
                pass  # referencing an exiting connector, no need to add again
            else:
                designators_and_templates[designator] = template
        return (template, designator)

    # utilities to check for alternating connectors and cables/arrows ==========

    alternating_types = ["connector", "cable/arrow"]
    expected_type = None

    def check_type(designator, template, actual_type):
        nonlocal expected_type
        if not expected_type:  # each connection set may start with either section
            expected_type = actual_type

        if actual_type != expected_type:  # did not alternate
            raise Exception(
                f'Expected {expected_type}, but "{designator}" ("{template}") is {actual_type}'
            )

    def alternate_type():  # flip between connector and cable/arrow
        nonlocal expected_type
        expected_type = alternating_types[1 - alternating_types.index(expected_type)]

    for connection_set in connection_sets:
        # figure out number of parallel connections within this set
        connectioncount = []
        for entry in connection_set:
            if isinstance(entry, list):
                connectioncount.append(len(entry))
            elif isinstance(entry, dict):
                connectioncount.append(len(expand(list(entry.values())[0])))
                # e.g.: - X1: [1-4,6] yields 5
            else:
                pass  # strings do not reveal connectioncount
        if not any(connectioncount):
            # no item in the list revealed connection count;
            # assume connection count is 1
            connectioncount = [1]
            # Example: The following is a valid connection set,
            #          even though no item reveals the connection count;
            #          the count is not needed because only a component-level mate happens.
            # -
            #   - CONNECTOR
            #   - ==>
            #   - CONNECTOR

        # check that all entries are the same length
        if len(set(connectioncount)) > 1:
            raise Exception(
                "All items in connection set must reference the same number of connections"
            )
        # all entries are the same length, connection count is set
        connectioncount = connectioncount[0]

        # expand string entries to list entries of correct length
        for index, entry in enumerate(connection_set):
            if isinstance(entry, str):
                connection_set[index] = [entry] * connectioncount

        # resolve all designators
        for index, entry in enumerate(connection_set):
            if isinstance(entry, list):
                for subindex, item in enumerate(entry):
                    template, designator = resolve_designator(item, template_separator_char)
                    connection_set[index][subindex] = designator
            elif isinstance(entry, dict):
                key = list(entry.keys())[0]
                template, designator = resolve_designator(key, template_separator_char)
                value = entry[key]
                connection_set[index] = {designator: value}
            else:
                pass  # string entries have been expanded in previous step

        # expand all pin lists
        for index, entry in enumerate(connection_set):
            if isinstance(entry, list):
                connection_set[index] = [{designator: 1} for designator in entry]
            elif isinstance(entry, dict):
                designator = list(entry.keys())[0]
                pinlist = expand(entry[designator])
                connection_set[index] = [{designator: pin} for pin in pinlist]
            else:
                pass  # string entries have been expanded in previous step

        # Populate wiring harness ==============================================

        expected_type = None  # reset check for alternating types
        # at the beginning of every connection set
        # since each set may begin with either type

        # generate components
        for entry in connection_set:
            for item in entry:
                designator = list(item.keys())[0]
                template = designators_and_templates[designator]

                if designator in harness.connectors:  # existing connector instance
                    check_type(designator, template, "connector")
                elif template in template_connectors.keys():
                    # generate new connector instance from template
                    check_type(designator, template, "connector")
                    harness.add_connector(name=designator, **template_connectors[template])

                elif designator in harness.cables:  # existing cable instance
                    check_type(designator, template, "cable/arrow")
                elif template in template_cables.keys():
                    # generate new cable instance from template
                    check_type(designator, template, "cable/arrow")
                    harness.add_cable(name=designator, **template_cables[template])

                elif is_arrow(designator):
                    check_type(designator, template, "cable/arrow")
                    # arrows do not need to be generated here
                else:
                    raise Exception(f"{template} is an unknown template/designator/arrow.")

            alternate_type()  # entries in connection set must alternate between connectors and cables/arrows

        # transpose connection set list
        # before: one item per component, one subitem per connection in set
        # after:  one item per connection in set, one subitem per component
        connection_set = list(map(list, zip(*connection_set)))

        # connect components
        for index_entry, entry in enumerate(connection_set):
            for index_item, item in enumerate(entry):
                designator = list(item.keys())[0]

                if designator in harness.cables:
                    if index_item == 0:
                        # list started with a cable, no connector to join on left side
                        from_name, from_pin = (None, None)
                    else:
                        from_name, from_pin = get_single_key_and_value(entry[index_item - 1])
                    via_name, via_pin = (designator, item[designator])
                    if index_item == len(entry) - 1:
                        # list ends with a cable, no connector to join on right side
                        to_name, to_pin = (None, None)
                    else:
                        to_name, to_pin = get_single_key_and_value(entry[index_item + 1])
                    harness.connect(from_name, from_pin, via_name, via_pin, to_name, to_pin)

                elif is_arrow(designator):
                    if index_item == 0:  # list starts with an arrow
                        raise Exception("An arrow cannot be at the start of a connection set")
                    elif index_item == len(entry) - 1:  # list ends with an arrow
                        raise Exception("An arrow cannot be at the end of a connection set")

                    from_name, from_pin = get_single_key_and_value(entry[index_item - 1])
                    via_name, via_pin = (designator, None)
                    to_name, to_pin = get_single_key_and_value(entry[index_item + 1])
                    if "-" in designator:  # mate pin by pin
                        harness.add_mate_pin(from_name, from_pin, to_name, to_pin, designator)
                    elif "=" in designator and index_entry == 0:
                        # mate two connectors as a whole
                        harness.add_mate_component(from_name, to_name, designator)

    # warn about unused templates

    proposed_components = list(template_connectors.keys()) + list(template_cables.keys())
    used_components = set(designators_and_templates.values())
    forgotten_components = [c for c in proposed_components if c not in used_components]
    if len(forgotten_components) > 0:
        print("Warning: The following components are not referenced in any connection set:")
        print(", ".join(forgotten_components))

    # harness population completed =============================================

    if "additional_bom_items" in yaml_data:
        for line in yaml_data["additional_bom_items"]:
            harness.add_bom_item(line)

    if output_formats:
        harness.output(filename=output_file, fmt=output_formats, view=False)

    if return_types:
        returns = []
        if isinstance(return_types, str):  # only one return type speficied
            return_types = [return_types]

        return_types = [t.lower() for t in return_types]

        for rt in return_types:
            if rt == "png":
                returns.append(harness.png)
            if rt == "svg":
                returns.append(harness.svg)
            if rt == "harness":
                returns.append(harness)

        return tuple(returns) if len(returns) != 1 else returns[0]


def _get_yaml_data_and_path(inp: Union[str, Path, dict]) -> tuple[dict, Path]:
    # determine whether inp is a file path, a YAML string, or a Dict
    if not isinstance(inp, Dict):  # received a str or a Path
        try:
            yaml_path = Path(inp).expanduser().resolve(strict=True)
            # if no FileNotFoundError exception happens, get file contents
            yaml_str = file_read_text(yaml_path)
        except (FileNotFoundError, OSError, ValueError) as e:
            # if inp is a long YAML string, Pathlib will normally raise
            # FileNotFoundError or OSError(errno = ENAMETOOLONG) when
            # trying to expand and resolve it as a path, but in Windows
            # might ValueError or OSError(errno = EINVAL or None) be raised
            # instead in some cases (depending on the Python version).
            # Catch these specific errors, but raise any others.

            from errno import EINVAL, ENAMETOOLONG

            if type(e) is OSError and e.errno not in (EINVAL, ENAMETOOLONG, None):
                print(f"OSError(errno={e.errno}) in Python {sys.version} at {platform.platform()}")
                raise e
            # file does not exist; assume inp is a YAML string
            yaml_str = inp
            yaml_path = None
        yaml_data = yaml.safe_load(yaml_str)
    else:
        # received a Dict, use as-is
        yaml_data = inp
        yaml_path = None
    return yaml_data, yaml_path


def _get_output_dir(input_file: Path, default_output_dir: Path) -> Path:
    if default_output_dir:  # user-specified output directory
        output_dir = Path(default_output_dir)
    else:  # auto-determine appropriate output directory
        if input_file:  # input comes from a file; place output in same directory
            output_dir = input_file.parent
        else:  # input comes from str or Dict; fall back to cwd
            output_dir = Path.cwd()
    return output_dir.resolve()


def _get_output_name(input_file: Path, default_output_name: Path) -> str:
    if default_output_name:  # user-specified output name
        output_name = default_output_name
    else:  # auto-determine appropriate output name
        if input_file:  # input comes from a file; use same file stem
            output_name = input_file.stem
        else:  # input comes from str or Dict; no fallback available
            raise Exception("No output file name provided")
    return output_name


def main():
    print("When running from the command line, please use wv_cli.py instead.")


if __name__ == "__main__":
    main()
