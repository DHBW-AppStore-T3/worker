"""
OpenStack service for image management
"""

import json
import logging
import os
import subprocess

logger = logging.getLogger(__name__)


class OpenStackService:
    """Service for OpenStack image and compute-instance operations.

    All methods shell out to the ``openstack`` CLI (python-openstackclient)
    rather than using the SDK in-process, mirroring how the rest of the
    worker invokes external tooling (Packer, Terraform). Exit-code zero
    is treated as success; stderr is captured and returned to the
    caller for surfacing in the per-deployment log.

    The CLI is configured via env vars (OS_AUTH_URL etc.) supplied by
    :class:`PerTaskCloudsConfig`. ``OS_CLIENT_CONFIG_FILE`` and
    ``OS_CLOUD`` make the CLI prefer the per-task ``clouds.yaml`` over
    any ambient configuration on the worker host.
    """

    def __init__(self, env_vars: dict[str, str] | None = None):
        """
        Initialize OpenStack service

        Args:
            env_vars: OpenStack environment variables (OS_AUTH_URL, OS_USERNAME, etc.)
        """
        self.env_vars = env_vars or {}

    def _run(self, args: list[str], timeout: int = 60) -> tuple[int, str, str]:
        """Run an ``openstack`` CLI command with the configured env vars.

        Centralised so every call carries the per-task credentials and
        the same error handling. Returns the raw triple
        ``(returncode, stdout, stderr)`` so callers can decide how to
        report; structured-output methods JSON-decode stdout themselves.
        """
        env = os.environ.copy()
        env.update(self.env_vars)
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
            return (result.returncode, result.stdout or "", result.stderr or "")
        except subprocess.TimeoutExpired:
            return (-1, "", f"Timeout after {timeout}s running: {' '.join(args)}")
        except FileNotFoundError:
            return (-1, "", "OpenStack CLI not found — install python-openstackclient")
        except Exception as e:  # noqa: BLE001 — surfaced to user
            return (-1, "", f"Error running openstack CLI: {e}")

    def check_image_exists(self, image_name: str) -> tuple[bool, str | None]:
        """
        Check if an image with the given name already exists in OpenStack

        Args:
            image_name: Name of the image to check

        Returns:
            tuple: (exists: bool, image_id: str | None)
        """
        if not self.env_vars.get("OS_AUTH_URL"):
            logger.warning("No OpenStack credentials available for image check")
            return (False, None)

        rc, stdout, stderr = self._run(
            ["openstack", "image", "list", "--name", image_name, "-f", "json"],
            timeout=30,
        )
        if rc != 0:
            logger.error(f"Failed to check image existence: {stderr}")
            return (False, None)

        try:
            images = json.loads(stdout)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse OpenStack CLI output: {e}")
            return (False, None)

        if images:
            image_id = images[0].get("ID")
            logger.info(f"Image '{image_name}' already exists with ID: {image_id}")
            return (True, image_id)
        logger.info(f"Image '{image_name}' does not exist")
        return (False, None)

    # ------------------------------------------------------------------
    # Compute instance lifecycle (used by pause / resume)
    # ------------------------------------------------------------------
    #
    # ``server stop`` / ``server start`` are idempotent at the CLI
    # level: stopping an already-SHUTOFF instance returns exit code 0
    # with no error, and the same applies to starting an already-ACTIVE
    # instance. This makes pause/resume safe to retry without extra
    # state checks. We surface the CLI's stderr verbatim so the user
    # can see the precise reason on the rare hard failure (locked task,
    # auth glitch, etc.).

    def server_show(self, server_id: str) -> dict | None:
        """Return the parsed ``openstack server show <id> -f json`` payload.

        Used to log the human-readable name and current power state
        before issuing a stop/start, so the per-deployment log shows
        ``"web-1 (ACTIVE) → stopping"`` rather than just a UUID.
        Returns ``None`` if the server can't be fetched (deleted,
        permission, transient failure).
        """
        rc, stdout, stderr = self._run(
            ["openstack", "server", "show", server_id, "-f", "json"],
            timeout=30,
        )
        if rc != 0:
            logger.warning(f"server show failed for {server_id}: {stderr.strip()}")
            return None
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            return None

    def server_stop(self, server_id: str) -> tuple[bool, str | None]:
        """Stop an OpenStack compute instance.

        Returns ``(True, None)`` on success, ``(False, stderr)`` on
        failure. The CLI is idempotent on already-SHUTOFF instances —
        no extra status check needed.
        """
        rc, _stdout, stderr = self._run(
            ["openstack", "server", "stop", server_id],
            timeout=120,
        )
        if rc == 0:
            return (True, None)
        return (False, stderr.strip() or "openstack server stop failed")

    def server_start(self, server_id: str) -> tuple[bool, str | None]:
        """Start an OpenStack compute instance.

        Returns ``(True, None)`` on success, ``(False, stderr)`` on
        failure. Idempotent on already-ACTIVE instances.
        """
        rc, _stdout, stderr = self._run(
            ["openstack", "server", "start", server_id],
            timeout=120,
        )
        if rc == 0:
            return (True, None)
        return (False, stderr.strip() or "openstack server start failed")

    # ------------------------------------------------------------------
    # Orphan reaping (used by cancel)
    # ------------------------------------------------------------------
    #
    # When a deploy is cancelled mid-flight, Packer never reaches its own
    # cleanup step, so the build instance, the temporary keypair it
    # generated and (rarely) a half-registered Glance image are left
    # behind. Terraform state covers nothing of this: the build instance
    # is Packer's, not Terraform's. These helpers let the cancel task
    # reap them by name.
    #
    # Every method is idempotent and never raises: cancel runs as a
    # best-effort sweep and one missing resource must not abort the rest.

    def servers_by_name(self, name: str) -> list[str]:
        """Return the IDs of every server with this exact name."""
        rc, stdout, stderr = self._run(
            ["openstack", "server", "list", "--name", name, "-f", "json"],
            timeout=30,
        )
        if rc != 0:
            logger.error(f"Failed to list servers named '{name}': {stderr}")
            return []
        try:
            return [s["ID"] for s in json.loads(stdout) if s.get("ID")]
        except (json.JSONDecodeError, TypeError) as e:
            logger.error(f"Failed to parse server list: {e}")
            return []

    def server_delete(self, server_id: str) -> tuple[bool, str | None]:
        """Delete a server and wait for it to disappear."""
        rc, _stdout, stderr = self._run(
            ["openstack", "server", "delete", "--wait", server_id],
            timeout=300,
        )
        if rc != 0:
            # Already gone is a success for our purposes.
            if "could not be found" in (stderr or "").lower() or "no server with" in (stderr or "").lower():
                return (True, None)
            logger.error(f"Failed to delete server {server_id}: {stderr}")
            return (False, stderr)
        return (True, None)

    def image_delete_by_name(self, image_name: str) -> tuple[bool, str | None]:
        """Delete a Glance image by exact name, if it exists.

        Only ever called for the image name this deployment's build was
        producing, so it cannot touch an unrelated app's image.
        """
        exists, image_id = self.check_image_exists(image_name)
        if not exists or not image_id:
            return (True, None)
        rc, _stdout, stderr = self._run(
            ["openstack", "image", "delete", image_id],
            timeout=120,
        )
        if rc != 0:
            logger.error(f"Failed to delete image {image_id}: {stderr}")
            return (False, stderr)
        return (True, None)

    def unused_packer_keypairs(self) -> list[str]:
        """Packer-generated keypairs that no server is still using.

        Packer names its throwaway keypair ``packer_<uuid>``. Deleting
        every match would race a build running in parallel, so we only
        return the ones no current server references.
        """
        rc, stdout, stderr = self._run(["openstack", "keypair", "list", "-f", "json"], timeout=30)
        if rc != 0:
            logger.error(f"Failed to list keypairs: {stderr}")
            return []
        try:
            names = [k["Name"] for k in json.loads(stdout) if str(k.get("Name", "")).startswith("packer_")]
        except (json.JSONDecodeError, TypeError) as e:
            logger.error(f"Failed to parse keypair list: {e}")
            return []
        if not names:
            return []

        rc, stdout, stderr = self._run(
            ["openstack", "server", "list", "--long", "-f", "json"],
            timeout=60,
        )
        if rc != 0:
            # Can't prove they are unused, so leave them alone.
            logger.warning(f"Could not list servers to check keypair usage: {stderr}")
            return []
        try:
            in_use = {s.get("Key Name") for s in json.loads(stdout)}
        except (json.JSONDecodeError, TypeError):
            return []
        return [n for n in names if n not in in_use]

    def keypair_delete(self, name: str) -> bool:
        """Delete a keypair by name. Missing is treated as success."""
        rc, _stdout, stderr = self._run(["openstack", "keypair", "delete", name], timeout=60)
        if rc != 0 and "could not be found" not in (stderr or "").lower():
            logger.error(f"Failed to delete keypair {name}: {stderr}")
            return False
        return True

    # ------------------------------------------------------------------
    # Image retention
    #
    # Every Packer build produces a new content-addressed image and the
    # old one is left behind: nothing in OpenStack expires them, and this
    # project has no image-storage quota to make it visible. It had
    # reached 340 GiB, of which 160 GiB was superseded Windows images no
    # deployment referenced.
    #
    # Both listers return None rather than an empty result when the CLI
    # call fails. The caller must treat None as "do not prune": an empty
    # in-use set would otherwise read as "nothing is in use" and delete
    # images that are.
    # ------------------------------------------------------------------

    def images_newest_first(self) -> list[tuple[str, str]] | None:
        """Return (id, name) for this project's private images, newest first.

        None means the listing failed and callers must not draw conclusions
        from it.
        """
        rc, stdout, stderr = self._run(
            [
                "openstack", "image", "list", "--private",
                "--sort", "created_at:desc",
                "-c", "ID", "-c", "Name", "-f", "json",
            ],
            timeout=60,
        )
        if rc != 0:
            logger.error(f"Failed to list images: {stderr}")
            return None
        try:
            return [(i["ID"], i["Name"]) for i in json.loads(stdout) if i.get("ID") and i.get("Name")]
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            logger.error(f"Failed to parse image list: {e}")
            return None

    def image_ids_in_use(self) -> set[str] | None:
        """Image IDs that existing servers were booted from.

        None means the listing failed - see the note above on why that is
        not the same as an empty set.
        """
        rc, stdout, stderr = self._run(
            ["openstack", "server", "list", "--long", "-f", "json"],
            timeout=60,
        )
        if rc != 0:
            logger.error(f"Failed to list servers for image usage: {stderr}")
            return None
        try:
            return {s["Image ID"] for s in json.loads(stdout) if s.get("Image ID")}
        except (json.JSONDecodeError, TypeError) as e:
            logger.error(f"Failed to parse server list: {e}")
            return None

    def image_delete_by_id(self, image_id: str) -> tuple[bool, str | None]:
        """Delete a Glance image by ID."""
        rc, _stdout, stderr = self._run(["openstack", "image", "delete", image_id], timeout=120)
        if rc != 0:
            logger.error(f"Failed to delete image {image_id}: {stderr}")
            return (False, stderr)
        return (True, None)
