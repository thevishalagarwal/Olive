# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
# --------------------------------------------------------------------------
import json
import re
import subprocess
from abc import ABC, abstractmethod
from argparse import ArgumentParser, Namespace
from pathlib import Path
from typing import ClassVar, Dict, Optional, Union

import yaml

from olive.cli.constants import CONDA_CONFIG
from olive.common.user_module_loader import UserModuleLoader
from olive.common.utils import hardlink_copy_dir, hash_dict, set_nested_dict_value
from olive.resource_path import OLIVE_RESOURCE_ANNOTATIONS, find_all_resources


class BaseOliveCLICommand(ABC):
    allow_unknown_args: ClassVar[bool] = False

    def __init__(self, parser: ArgumentParser, args: Namespace, unknown_args: Optional[list] = None):
        self.args = args
        self.unknown_args = unknown_args

        if unknown_args and not self.allow_unknown_args:
            parser.error(f"Unknown arguments: {unknown_args}")

    @staticmethod
    @abstractmethod
    def register_subcommand(parser: ArgumentParser):
        raise NotImplementedError

    @abstractmethod
    def run(self):
        raise NotImplementedError


def _get_hf_input_model(args: Namespace, model_path: OLIVE_RESOURCE_ANNOTATIONS) -> Dict:
    """Get the input model config for HuggingFace model.

    args.task is optional.
    args.adapter_path might not be present.
    """
    print(f"Loading HuggingFace model from {model_path}")
    input_model = {
        "type": "HfModel",
        "model_path": model_path,
        "load_kwargs": {
            "trust_remote_code": args.trust_remote_code,
            "attn_implementation": "eager",
        },
    }
    if args.task:
        input_model["task"] = args.task
    if getattr(args, "adapter_path", None):
        input_model["adapter_path"] = args.adapter_path
    return input_model


def _get_onnx_input_model(model_path: str) -> Dict:
    """Get the input model config for ONNX model.

    Only supports local ONNX model file path.
    """
    print(f"Loading ONNX model from {model_path}")
    model_config = {
        "type": "OnnxModel",
        "model_path": model_path,
    }

    # additional processing for the model folder
    model_path = Path(model_path).resolve()
    if model_path.is_dir():
        onnx_files = list(model_path.glob("*.onnx"))
        if len(onnx_files) > 1:
            raise ValueError("Found multiple .onnx model files in the model folder. Please specify one.")
        onnx_file_path = onnx_files[0]
        model_config["onnx_file_name"] = onnx_file_path.name

        # all files other than the .onnx file and .onnx.data file considered as additional files
        additional_files = sorted(
            set({str(fp) for fp in model_path.iterdir()} - {str(onnx_file_path), str(onnx_file_path) + ".data"})
        )
        if additional_files:
            model_config["model_attributes"] = {"additional_files": additional_files}

    return model_config


def _get_pt_input_model(args: Namespace, model_path: OLIVE_RESOURCE_ANNOTATIONS) -> Dict:
    """Get the input model config for PyTorch model.

    args.model_script is required.
    model_path is optional.
    """
    if not args.model_script:
        raise ValueError("model_script is not provided. Either model_name_or_path or model_script is required.")

    user_module_loader = UserModuleLoader(args.model_script, args.script_dir)

    if not model_path and not user_module_loader.has_function("_model_loader"):
        raise ValueError("Either _model_loader or model_name_or_path is required for PyTorch model.")

    input_model_config = {
        "type": "PyTorchModel",
        "model_script": args.model_script,
    }

    if args.script_dir:
        input_model_config["script_dir"] = args.script_dir

    if model_path:
        print("Loading PyTorch model from", model_path)
        input_model_config["model_path"] = model_path

    if user_module_loader.has_function("_model_loader"):
        print("Loading PyTorch model from function: _model_loader.")
        input_model_config["model_loader"] = "_model_loader"

    model_funcs = [
        ("io_config", "_io_config"),
        ("dummy_inputs_func", "_dummy_inputs"),
        ("model_file_format", "_model_file_format"),
    ]
    input_model_config.update(
        {config_key: func_name for config_key, func_name in model_funcs if user_module_loader.has_function(func_name)}
    )

    if "io_config" not in input_model_config and "dummy_inputs_func" not in input_model_config:
        raise ValueError("_io_config or _dummy_inputs is required in the script for PyTorch model.")
    return input_model_config


def get_input_model_config(args: Namespace) -> Dict:
    """Parse the model_name_or_path and return the input model config.

    Check model_name_or_path formats in order:
    1. Local PyTorch model with model loader but no model path
    2. Output of a previous command
    3. azureml:<model_name>:<version> (only for PyTorch model)
    4. Load PyTorch model with model_script
    5. azureml://registries/<registry_name>/models/<model_name>/versions/<version> (only for HF model)
    6. https://huggingface.co/<model_name> (only for HF model)
    7. HF model name string
    8. local file path
      a. local onnx model file path (either a user-provided model or a model produced by the Olive CLI)
      b. local HF model file path (either a user-provided model or a model produced by the Olive CLI)
    """
    model_name_or_path = args.model_name_or_path

    if model_name_or_path is None:
        if hasattr(args, "model_script"):
            # pytorch model with model_script, model_path is optional
            print("model_name_or_path is not provided. Using model_script to load the model.")
            return _get_pt_input_model(args, None)
        raise ValueError("model_name_or_path is required.")

    model_path = Path(model_name_or_path)
    # check if is the output of a previous command
    if model_path.is_dir() and (model_path / "model_config.json").exists():
        with open(model_path / "model_config.json") as f:
            model_config = json.load(f)

        adapter_path = getattr(args, "adapter_path", None)
        if adapter_path:
            assert model_config["type"].lower() == "hfmodel", "Only HfModel supports adapter_path."
            model_config["config"]["adapter_path"] = adapter_path

        print(f"Loaded previous command output of type {model_config['type']} from {model_name_or_path}")
        return model_config

    # Check AzureML model
    pattern = r"^azureml:(?P<model_name>[^:]+):(?P<version>[^:]+)$"
    match = re.match(pattern, model_name_or_path)
    if match:
        return _get_pt_input_model(
            args,
            {
                "type": "azureml_model",
                "name": match.group("model_name"),
                "version": match.group("version"),
            },
        )

    if getattr(args, "model_script", None):
        return _get_pt_input_model(args, model_name_or_path)

    # Check AzureML Registry model
    pattern = (
        r"^azureml://registries/(?P<registry_name>[^/]+)/models/(?P<model_name>[^/]+)/versions/(?P<version>[^/]+)$"
    )
    match = re.match(pattern, model_name_or_path)
    if match:
        return _get_hf_input_model(
            args,
            {
                "type": "azureml_registry_model",
                "registry_name": match.group("registry_name"),
                "name": match.group("model_name"),
                "version": match.group("version"),
            },
        )

    # Check HuggingFace url
    pattern = r"https://huggingface\.co/([^/]+/[^/]+)(?:/.*)?"
    match = re.search(pattern, model_name_or_path)
    if match:
        return _get_hf_input_model(args, match.group(1))

    # Check HF model name string
    if not model_path.exists():
        try:
            from huggingface_hub import repo_exists
        except ImportError as e:
            raise ImportError("Please install huggingface_hub to use the CLI for Huggingface model.") from e

        if not repo_exists(model_name_or_path):
            raise ValueError(f"{model_name_or_path} is not a valid Huggingface model name.")
        return _get_hf_input_model(args, model_name_or_path)

    # Check local onnx file/folder (user-provided model)
    if (model_path.is_file() and model_path.suffix == ".onnx") or any(model_path.glob("*.onnx")):
        return _get_onnx_input_model(model_name_or_path)

    # Check local HF model file (user-provided model)
    return _get_hf_input_model(args, model_name_or_path)


def add_logging_options(sub_parser: ArgumentParser):
    """Add logging options to the sub_parser."""
    log_group = sub_parser.add_argument_group("logging options")
    log_group.add_argument(
        "--log_level",
        type=int,
        default=3,
        help="Logging level. Default is 3. level 0: DEBUG, 1: INFO, 2: WARNING, 3: ERROR, 4: CRITICAL",
    )


def add_remote_options(sub_parser: ArgumentParser):
    """Add remote options to the sub_parser."""
    remote_group = sub_parser.add_argument_group("remote options")
    remote_group.add_argument(
        "--resource_group",
        type=str,
        required=False,
        help="Resource group for the AzureML workspace.",
    )
    remote_group.add_argument(
        "--workspace_name",
        type=str,
        required=False,
        help="Workspace name for the AzureML workspace.",
    )
    remote_group.add_argument(
        "--keyvault_name",
        type=str,
        required=False,
        help=(
            "The azureml keyvault name with huggingface token to use for remote run. Refer to"
            " https://microsoft.github.io/Olive/features/huggingface_model_optimization.html#huggingface-login for"
            " more details."
        ),
    )
    remote_group.add_argument(
        "--aml_compute",
        type=str,
        required=False,
        help="The compute name to run the workflow on.",
    )


def add_model_options(
    sub_parser: ArgumentParser,
    enable_hf: bool = False,
    enable_hf_adapter: bool = False,
    enable_pt: bool = False,
    enable_onnx: bool = False,
    default_output_path: Optional[str] = None,
):
    """Add model options to the sub_parser.

    Use enable_hf, enable_hf_adapter, enable_pt, enable_onnx to enable the corresponding model options.
    If default_output_path is None, it is required to provide the output_path.
    """
    assert any([enable_hf, enable_hf_adapter, enable_pt, enable_onnx]), "At least one model option should be enabled."

    model_group = sub_parser.add_argument_group("Model options")

    m_description = (
        "Path to the input model. Can the be output of a previous command or a standalone model. For standalone models,"
        " the following formats are supported:\n"
    )
    if enable_hf:
        m_description += (
            " HfModel: The name or path to the model. Local folder, huggingface id, or AzureML Registry model"
            " (azureml://registries/<registry_name>/models/<model_name>/versions/<version>).\n"
        )
    if enable_pt:
        m_description += (
            " PyTorchModel: Path to the PyTorch model. Local file/folder or AzureML model"
            " (azureml:<model_name>:<version>).\n"
        )
    if enable_onnx:
        m_description += " OnnxModel: Path to the ONNX model. Local file/folder.\n"

    model_group.add_argument(
        "-m",
        "--model_name_or_path",
        type=str,
        help=m_description,
    )
    if enable_hf:
        model_group.add_argument(
            "--trust_remote_code", action="store_true", help="Trust remote code when loading a model."
        )
        model_group.add_argument("-t", "--task", type=str, help="Task for which the model is used.")
    if enable_hf_adapter:
        assert enable_hf, "enable_hf must be True when enable_hf_adapter is True."
        model_group.add_argument(
            "--adapter_path",
            type=str,
            help="Path to the adapters weights saved after peft fine-tuning. Local folder or huggingface id.",
        )
    if enable_pt:
        model_group.add_argument(
            "--model_script",
            type=str,
            help="The script file containing the model definition. Required for PyTorch model.",
        )
        model_group.add_argument(
            "--script_dir",
            type=str,
            help="The directory containing the model script file.",
        )

    model_group.add_argument(
        "-o",
        "--output_path",
        type=output_path_type,
        required=default_output_path is None,
        default=default_output_path,
        help="Path to save the command output.",
    )


def output_path_type(path: str) -> str:
    """Resolve the output path and mkdir if it doesn't exist."""
    path = Path(path).resolve()

    if path.exists():
        assert path.is_dir(), f"{path} is not a directory."

    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def is_remote_run(args: Namespace) -> bool:
    """Check if the run is a remote run."""
    return all([args.resource_group, args.workspace_name, args.aml_compute])


def update_remote_option(config: Dict, args: Namespace, cli_action: str, tempdir: Union[str, Path]):
    """Update the config for remote run."""
    if args.resource_group or args.workspace_name or args.aml_compute:
        if not is_remote_run(args):
            raise ValueError("resource_group, workspace_name and aml_compute are required for remote workflow run.")

        config["workflow_id"] = f"{cli_action}-{hash_dict(config)}"

        try:
            subscription_id = json.loads(subprocess.check_output("az account show", shell=True).decode("utf-8"))["id"]
            print("Using Azure subscription ID: %s", subscription_id)

        except subprocess.CalledProcessError:
            print(
                "Error: Unable to retrieve account information. "
                "Make sure you are logged in to Azure CLI with command `az login`."
            )

        config["azureml_client"] = {
            "subscription_id": subscription_id,
            "resource_group": args.resource_group,
            "workspace_name": args.workspace_name,
            "keyvault_name": args.keyvault_name,
            "default_auth_params": {"exclude_managed_identity_credential": True},
        }

        conda_file_path = Path(tempdir) / "conda_gpu.yaml"
        with open(conda_file_path, "w") as f:
            yaml.dump(CONDA_CONFIG, f)

        config["systems"]["aml_system"] = {
            "type": "AzureML",
            "accelerators": [{"device": "GPU", "execution_providers": ["CUDAExecutionProvider"]}],
            "aml_compute": args.aml_compute,
            "aml_docker_config": {
                "base_image": "mcr.microsoft.com/azureml/openmpi4.1.0-cuda11.8-cudnn8-ubuntu22.04",
                "conda_file_path": str(conda_file_path),
            },
            "hf_token": bool(args.keyvault_name),
        }
        config["workflow_host"] = "aml_system"


# TODO(anyone): Consider using the footprint directly to save the model
def save_output_model(config: Dict, output_model_dir: Union[str, Path]):
    """Save the output model to the output_model_dir.

    This assumes a single accelerator workflow.
    """
    run_output_path = Path(config["output_dir"]) / "output_model"
    if not any(run_output_path.rglob("model_config.json")):
        # there must be an run_output_path with at least one model_config.json
        print("Command failed. Please set the log_level to 1 for more detailed logs.")
        return

    output_model_dir = Path(output_model_dir).resolve()

    # hardlink/copy the output model to the output_model_dir
    hardlink_copy_dir(run_output_path, output_model_dir)

    # need to update the local path in the model_config.json
    # should the path be relative or absolute? relative makes it easy to move the output
    # around but the path needs to be updated when the model config is used
    for model_config_file in output_model_dir.rglob("model_config.json"):
        with model_config_file.open("r") as f:
            model_config = json.load(f)

        all_resources = find_all_resources(model_config)
        for resource_key, resource_path in all_resources.items():
            resource_path_str = resource_path.get_path()
            if resource_path_str.startswith(str(run_output_path)):
                set_nested_dict_value(
                    model_config,
                    resource_key,
                    resource_path_str.replace(str(run_output_path), str(output_model_dir)),
                )

        with model_config_file.open("w") as f:
            json.dump(model_config, f, indent=4)

    print(f"Command succeeded. Output model saved to {output_model_dir}")
