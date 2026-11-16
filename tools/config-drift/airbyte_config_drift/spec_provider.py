"""Spec providers: how the tool obtains a connector's current spec."""

from __future__ import annotations

import json
import subprocess
from typing import Mapping, Optional, Protocol

DEFAULT_SPEC_TIMEOUT_SECONDS = 60.0
# Cap on raw `spec` command output, matching the cache's spec size bound.
DEFAULT_MAX_OUTPUT_BYTES = 2_000_000


class SpecFetchError(RuntimeError):
    """Raised when a connector spec cannot be retrieved.

    The detector converts this into a BLOCKED report (fail safe) rather
    than dropping or migrating the stored configuration.
    """


class SpecProvider(Protocol):
    def get_spec(self, docker_repository: str, docker_image_tag: str) -> dict:
        """Return the connector's ``connectionSpecification`` dict."""
        ...


class StaticSpecProvider:
    """In-memory provider, primarily for tests and offline analysis."""

    def __init__(self, specs: Optional[Mapping[tuple[str, str], dict]] = None) -> None:
        self._specs = dict(specs or {})
        self.calls: list[tuple[str, str]] = []

    def set_spec(self, docker_repository: str, docker_image_tag: str, spec: dict) -> None:
        self._specs[(docker_repository, docker_image_tag)] = spec

    def get_spec(self, docker_repository: str, docker_image_tag: str) -> dict:
        self.calls.append((docker_repository, docker_image_tag))
        key = (docker_repository, docker_image_tag)
        if key not in self._specs:
            raise SpecFetchError(f"no spec registered for {docker_repository}:{docker_image_tag}")
        return self._specs[key]


class DockerSpecProvider:
    """Fetches specs by running ``docker run --rm <image> spec``.

    Enforces a timeout and an output size cap; any failure raises
    :class:`SpecFetchError`.
    """

    def __init__(
        self,
        timeout_seconds: float = DEFAULT_SPEC_TIMEOUT_SECONDS,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        docker_binary: str = "docker",
    ) -> None:
        self._timeout = timeout_seconds
        self._max_output = max_output_bytes
        self._docker = docker_binary

    def get_spec(self, docker_repository: str, docker_image_tag: str) -> dict:
        image = f"{docker_repository}:{docker_image_tag}"
        try:
            proc = subprocess.run(
                [self._docker, "run", "--rm", image, "spec"],
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SpecFetchError(f"timed out after {self._timeout}s fetching spec for {image}") from exc
        except OSError as exc:
            raise SpecFetchError(f"failed to run docker for {image}: {exc}") from exc

        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace")[:500]
            raise SpecFetchError(f"`docker run {image} spec` exited {proc.returncode}: {stderr}")
        if len(proc.stdout) > self._max_output:
            raise SpecFetchError(
                f"spec output for {image} is {len(proc.stdout)} bytes, over the "
                f"{self._max_output}-byte cap"
            )
        try:
            message = json.loads(proc.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SpecFetchError(f"invalid JSON in spec output for {image}: {exc}") from exc

        # Connectors emit an AirbyteMessage of type SPEC wrapping the spec.
        spec = message.get("spec", message) if isinstance(message, dict) else None
        if not isinstance(spec, dict) or "connectionSpecification" not in spec:
            raise SpecFetchError(f"spec output for {image} has no connectionSpecification")
        return spec["connectionSpecification"]
