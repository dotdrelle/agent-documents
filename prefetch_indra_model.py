"""Fetch the pinned Indra model and license while building the Docker image."""

from __future__ import annotations

import shutil
from pathlib import Path

from huggingface_hub import hf_hub_download
from indra_ocr import load
from indra_ocr.models import model_dir


MODEL_REVISION = "v1.1.0"
MODEL_DIRECTORY = Path("/opt/indra-model")
LICENSE_DIRECTORY = Path("/usr/share/licenses/indra-ocr")


def main() -> None:
    cached_model = model_dir(revision=MODEL_REVISION)
    MODEL_DIRECTORY.mkdir(parents=True, exist_ok=True)
    for model_file in cached_model.iterdir():
        if model_file.is_file():
            shutil.copyfile(model_file, MODEL_DIRECTORY / model_file.name)

    # Loading the converter during the build verifies that the copied files
    # are sufficient. Runtime also sets HF_HUB_OFFLINE=1 and points directly
    # at this directory, so a missing file fails locally instead of downloading.
    load(path=MODEL_DIRECTORY, threads=1, mode="auto", image_ocr=True)

    license_file = hf_hub_download(
        "datalab-to/indra-ocr",
        "LICENSE",
        revision=MODEL_REVISION,
    )
    LICENSE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(license_file, LICENSE_DIRECTORY / "LICENSE")


if __name__ == "__main__":
    main()
