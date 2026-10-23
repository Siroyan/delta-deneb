"""Package only API source and locked, pure-Python AWS SDK dependencies offline."""

from importlib.metadata import distribution
from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DISTRIBUTIONS = (
    "boto3", "botocore", "s3transfer", "jmespath", "python-dateutil", "six", "urllib3",
)


def package_api(destination=None):
    destination = Path(destination) if destination else ROOT / "build" / "telemetry-api"
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    for name in ("health", "telemetry"):
        shutil.copytree(ROOT / "backend" / name, destination / "backend" / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
    (destination / "backend" / "__init__.py").touch()
    for name in RUNTIME_DISTRIBUTIONS:
        installed = distribution(name)
        for relative in installed.files or []:
            # Exclude console scripts outside site-packages and local bytecode.
            if ".." in relative.parts or "__pycache__" in relative.parts or relative.suffix in {".pyc", ".pyo"}:
                continue
            source = Path(installed.locate_file(relative))
            if not source.is_file():
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    return destination


if __name__ == "__main__":
    print(f"Packaged API at {package_api()}; AWS was not contacted.")
