#!/usr/bin/env python
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
import requests

import click
import ollama

import modules.annotations as annotations
import modules.drawing as drawing
import modules.fileparser as fileparser
import modules.graphmaker as graphmaker
import modules.helpers as helpers
import modules.interpreter as interpreter
import modules.tfwrapper as tfwrapper
import modules.cloud_config as cloud_config


__version__ = "0.8"


def my_excepthook(exc_type, exc_value, exc_traceback):
    print(f"Unhandled error: {exc_type}, {exc_value}, {exc_traceback}")


def _show_banner():
    banner = (
        "\n\n\n"
        " _____                          _     _             \n"
        "/__   \\___ _ __ _ __ __ ___   _(_)___(_) ___  _ __  \n"
        "  / /\\/ _ \\ '__| '__/ _` \\ \\ / / / __| |/ _ \\| '_ \\ \n"
        " / / |  __/ |  | | | (_| |\\ V /| \\__ \\ | (_) | | | |\n"
        " \\/   \\___|_|  |_|  \\__,_| \\_/ |_|___/_|\\___/|_| |_|\n"
        "                                                    \n"
        "\n"
    )
    print(banner)


def _validate_source(source: list):
    if source[0].endswith(".tf"):
        click.echo(
            click.style(
                "\nERROR: You have passed a .tf file as source. Please pass a folder containing .tf files or a git URL.\n",
                fg="red",
                bold=True,
            )
        )
        sys.exit()


def _load_json_source(source: str):
    click.echo(
        "Source is a pre-generated JSON tfdata or tfgraph file. Will not call terraform binary."
    )
    with open(source, "r") as file:
        jsondata = json.load(file)
    tfdata = {"annotations": {}, "meta_data": {}}
    if "all_resource" in jsondata:
        tfdata = jsondata
        tfdata["graphdict"] = dict(tfdata["original_graphdict"])
        return tfdata, False
    else:
        tfdata["graphdict"] = jsondata
        return tfdata, True


def _process_terraform_source(
    source: list, varfile: list, workspace: str, annotate: str, debug: bool
):
    tfdata = tfwrapper.tf_initplan(source, varfile, workspace)
    tfdata = tfwrapper.tf_makegraph(tfdata)
    codepath = (
        [tfdata["codepath"]]
        if isinstance(tfdata["codepath"], str)
        else tfdata["codepath"]
    )
    tfdata = fileparser.read_tfsource(codepath, varfile, annotate, tfdata)
    tfdata = interpreter.prefix_module_names(tfdata)
    tfdata = interpreter.resolve_all_variables(tfdata, debug)
    if debug:
        helpers.export_tfdata(tfdata)
    return tfdata


def _enrich_graph_data(tfdata: dict):
    tfdata = graphmaker.add_relations(tfdata)
    tfdata = graphmaker.consolidate_nodes(tfdata)
    tfdata = annotations.add_annotations(tfdata)
    tfdata = graphmaker.handle_special_resources(tfdata)
    tfdata = graphmaker.handle_variants(tfdata)
    tfdata = graphmaker.create_multiple_resources(tfdata)
    tfdata = graphmaker.reverse_relations(tfdata)
    tfdata = helpers.remove_recursive_links(tfdata)
    return tfdata


def _print_graph_debug(outputdict: dict, title: str):
    click.echo(click.style(f"\n{title}:\n", fg="white", bold=True))
    click.echo(json.dumps(outputdict, indent=4, sort_keys=True))


def compile_tfdata(
    source: list, varfile: list, workspace: str, debug: bool, annotate=""
):
    """Compile Terraform data from source files into enriched graph dictionary.

    Args:
        source: List of source paths (folders, git URLs, or JSON files)
        varfile: List of paths to .tfvars files
        workspace: Terraform workspace name
        debug: Enable debug output and export tracedata
        annotate: Path to custom annotations YAML file

    Returns:
        dict: Enriched tfdata dictionary with graphdict and metadata
    """
    _validate_source(source)
    if source[0].endswith(".json"):
        tfdata, already_processed = _load_json_source(source[0])
        if already_processed:
            debug = False
            _print_graph_debug(tfdata["graphdict"], "Loaded JSON graph dictionary")
            return tfdata
        else:
            _print_graph_debug(
                tfdata["original_graphdict"], "Unprocessed Terraform graph dictionary"
            )
            _print_graph_debug(
                tfdata["original_graphdict"], "Enriched graph dictionary"
            )
    else:
        tfdata = _process_terraform_source(source, varfile, workspace, annotate, debug)
    _print_graph_debug(
        tfdata["original_graphdict"], "Unprocessed Terraform graph dictionary"
    )
    tfdata = _enrich_graph_data(tfdata)
    tfdata["graphdict"] = helpers.sort_graphdict(tfdata["graphdict"])
    _print_graph_debug(tfdata["graphdict"], "Enriched graphviz dictionary")
    return tfdata


# amazonq-ignore-next-line


def _check_dependencies() -> None:
    """Check if required command-line tools are available."""
    dependencies = ["dot", "gvpr", "git", "terraform"]
    bundle_dir = Path(__file__).parent
    sys.path.append(str(bundle_dir))
    for exe in dependencies:
        location = shutil.which(exe) or os.path.isfile(exe)
        if location:
            click.echo(f"  {exe} command detected: {location}")
        else:
            click.echo(
                click.style(
                    f"\n  ERROR: {exe} command executable not detected in path. Please ensure you have installed all required dependencies first",
                    fg="red",
                    # amazonq-ignore-next-line
                    bold=True,
                )
                # amazonq-ignore-next-line
            )
            sys.exit()


def _check_terraform_version() -> None:
    """Validate Terraform version is compatible."""
    click.echo(click.style("\nChecking Terraform Version...", fg="white", bold=True))
    version_file = "terraform_version.txt"

    try:
        result = subprocess.run(
            ["terraform", "-v"], capture_output=True, text=True, check=True
        )
        version_output = result.stdout

        version_line = version_output.split("\n")[0]
        print(f"\n{version_line}")
        version = version_line.split(" ")[1].replace("v", "")
        version_major = version.split(".")[0]

        if version_major != "1":
            click.echo(
                click.style(
                    f"\n  ERROR: Terraform Version '{version}' is not supported. Please upgrade to >= v1.0.0",
                    fg="red",
                    bold=True,
                )
            )
            sys.exit()
    except (subprocess.CalledProcessError, IndexError, FileNotFoundError) as e:
        click.echo(
            click.style(
                f"\n  ERROR: Failed to check Terraform version: {e}",
                fg="red",
                bold=True,
            )
        )
        sys.exit()


def _check_ollama_server() -> None:
    """Check if Ollama server is reachable."""
    click.echo(click.style("\nChecking Ollama Server...", fg="white", bold=True))
    try:
        response = requests.get(f"{cloud_config.OLLAMA_HOST}/api/tags", timeout=5)
        if response.status_code == 200:
            click.echo(f"  Ollama server reachable at: {cloud_config.OLLAMA_HOST}")
        else:
            click.echo(
                click.style(
                    f"\n  ERROR: Ollama server returned status {response.status_code}",
                    fg="red",
                    bold=True,
                )
            )
            sys.exit()
    except requests.exceptions.RequestException as e:
        click.echo(
            click.style(
                f"\n  ERROR: Cannot reach Ollama server at {cloud_config.OLLAMA_HOST}: {e}",
                fg="red",
                bold=True,
            )
        )
        sys.exit()


def preflight_check() -> None:
    """Check required dependencies and Terraform version compatibility."""
    click.echo(click.style("\nPreflight check..", fg="white", bold=True))
    _check_dependencies()
    _check_terraform_version()
    _check_ollama_server()


@click.version_option(version=__version__, prog_name="terravision")
@click.group()
def cli():
    """
    TerraVision generates cloud architecture diagrams and documentation from Terraform scripts

    For help with a specific command type:

    terravision [COMMAND] --help

    """
    pass


def _create_llm_client():
    """Create and return Ollama LLM client."""
    return ollama.Client(
        host=cloud_config.OLLAMA_HOST, headers={"x-some-header": "some-value"}
    )


def _stream_llm_response(client, graphdict: dict) -> str:
    """Stream LLM response and return complete output."""
    stream = client.chat(
        model="llama3",
        keep_alive=-1,
        messages=[
            {
                "role": "user",
                "content": cloud_config.AWS_REFINEMENT_PROMPT + "\n" + str(graphdict),
            }
        ],
        stream=True,
    )
    full_response = ""
    for chunk in stream:
        content = chunk["message"]["content"]
        print(content, end="", flush=True)
        full_response += content
    return full_response


def _refine_with_llm(tfdata: dict) -> dict:
    """Refine graph dictionary using LLM and return updated tfdata."""
    click.echo(
        click.style("\nCalling AI Model for JSON refinement..\n", fg="white", bold=True)
    )
    client = _create_llm_client()
    full_response = _stream_llm_response(client, tfdata["graphdict"])
    refined_json = helpers.extract_json_from_string(full_response)
    _print_graph_debug(refined_json, "Final LLM Refined JSON")
    tfdata["graphdict"] = refined_json
    return tfdata


@cli.command()
@click.option("--debug", is_flag=True, default=False, help="Dump exception tracebacks")
@click.option(
    "--source",
    multiple=True,
    default=["."],
    help="Source files location (Git URL, Folder or .JSON file)",
)
@click.option(
    "--workspace",
    multiple=False,
    default="default",
    help="The Terraform workspace to initialise",
)
@click.option(
    # amazonq-ignore-next-line
    "--varfile",
    multiple=True,
    default=[],
    help="Path to .tfvars variables file",
)
@click.option(
    "--outfile",
    default="architecture",
    help="Filename for output diagram (default architecture.dot.png)",
)
@click.option("--format", default="png", help="File format (png/pdf/svg/bmp)")
@click.option(
    "--show", is_flag=True, default=False, help="Show diagram after generation"
)
@click.option(
    "--simplified",
    is_flag=True,
    default=False,
    help="Simplified high level services shown only",
)
@click.option("--annotate", default="", help="Path to custom annotations file (YAML)")
@click.option("--avl_classes", hidden=True)
def draw(
    debug,
    source,
    workspace,
    varfile,
    outfile,
    format,
    show,
    simplified,
    annotate,
    avl_classes,
):
    """Draws Architecture Diagram"""
    if not debug:
        sys.excepthook = my_excepthook
    _show_banner()
    preflight_check()
    tfdata = compile_tfdata(source, varfile, workspace, debug, annotate)
    tfdata = _refine_with_llm(tfdata)
    drawing.render_diagram(tfdata, show, simplified, outfile, format, source)


@cli.command()
@click.option("--debug", is_flag=True, default=False, help="Dump exception tracebacks")
@click.option(
    "--source",
    multiple=True,
    default=["."],
    help="Source files location (Git URL or folder)",
)
@click.option(
    "--workspace",
    multiple=False,
    default="default",
    help="The Terraform workspace to initialise",
)
@click.option(
    "--varfile", multiple=True, default=[], help="Path to .tfvars variables file"
)
@click.option(
    "--show_services",
    is_flag=True,
    default=False,
    help="Only show unique list of cloud services actually used",
)
@click.option(
    "--outfile",
    default="architecture",
    help="Filename for output list (default architecture.json)",
)
@click.option("--annotate", default="", help="Path to custom annotations file (YAML)")
@click.option("--avl_classes", hidden=True)
def graphdata(
    debug,
    source,
    varfile,
    workspace,
    show_services,
    annotate,
    avl_classes,
    outfile="graphdata.json",
):
    """List Cloud Resources and Relations as JSON"""
    if not debug:
        sys.excepthook = my_excepthook

    _show_banner()
    preflight_check()
    tfdata = compile_tfdata(source, varfile, workspace, debug, annotate)
    click.echo(click.style("\nOutput JSON Dictionary :", fg="white", bold=True))
    unique = helpers.unique_services(tfdata["graphdict"])
    click.echo(
        json.dumps(
            tfdata["graphdict"] if not show_services else unique,
            indent=4,
            sort_keys=True,
        )
    )
    if not outfile.endswith(".json"):
        outfile += ".json"
    click.echo(f"\nExporting graph object into file {outfile}")
    with open(outfile, "w") as f:
        json.dump(
            tfdata["graphdict"] if not show_services else unique,
            f,
            indent=4,
            sort_keys=True,
        )
    click.echo("\nCompleted!")


if __name__ == "__main__":
    cli(
        default_map={
            "draw": {"avl_classes": dir()},
            "graphlist": {"avl_classes": dir()},
        }
    )
