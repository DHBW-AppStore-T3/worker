"""Whether a deployment's Packer images may go after its destroy (dhbw-appstore).

Our images are named after the app and a hash of its ``packer/`` tree and
Packer variables (``tasks._build_image_names``), so deployments of the same
app in the same project can share one, across commits too. A destroy deletes
an image only when no other live deployment of that app is left in the
project (here) and no server in the project was booted from it (asked from
OpenStack by the caller).

"Live" is the API's view ``worker_live_deployments``: not hidden and no
successful destroy (a failed destroy still has its servers). It is the only
thing of the deployments the worker role may read. Asked under the image's
``PackerBuildLock``: a deploy that reuses the image holds the same lock while
it checks Glance, and its deployment row exists before its job runs, so the
two cannot miss each other.
"""

from __future__ import annotations

from .. import db


def others_use_app_images(deployment_id: str, app_id: str, project_id: str | None) -> bool:
    """True if another live deployment of ``app_id`` exists in ``project_id``.

    Also True without a project (an old deployment not yet placed): keep the
    image rather than guess.
    """
    if not project_id:
        return True
    with db.connect("image-usage") as conn:
        row = conn.execute(
            """SELECT 1 FROM worker_live_deployments
                WHERE "appId" = %s AND os_project_id = %s AND "deploymentId" <> %s
                LIMIT 1""",
            (app_id, project_id, deployment_id),
        ).fetchone()
    return row is not None
