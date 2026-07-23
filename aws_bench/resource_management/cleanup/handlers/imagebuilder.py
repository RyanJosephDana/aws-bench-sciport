"""EC2 Image Builder cleanup handlers.

Tasks that build an AMI via Image Builder leave a dependency chain the post-run
reset cannot clear with raw CCAPI:

* ``AWS::ImageBuilder::Component`` delete fails with "Resource dependency error:
  The resource ARN dependency constraint prevents deletion" because image
  recipes (and the images built from them) still reference the component.
* ``AWS::ImageBuilder::Image`` was "not attempted" — CCAPI does not delete the
  image build versions.

The component prepare handler walks the dependency graph in the only order AWS
allows — image build versions, then the recipes that reference the component —
so the component itself becomes deletable. The image handler deletes the build
version directly via the imagebuilder API. Only ``Self``-owned resources are
touched, never AWS/Amazon-managed components.
"""

from __future__ import annotations

import boto3
from botocore.client import BaseClient
from botocore.exceptions import BotoCoreError, ClientError

from aws_bench.logging.logger import get_logger
from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handler_registry import resource_handler
from aws_bench.resource_management.cleanup.handlers._service_delete import (
    prepare_error_result,
    service_delete,
)
from aws_bench.resource_management.cleanup.models import HandlerResult, HandlerStatus
from aws_bench.utils.concurrent import build_client

logger = get_logger(__name__)

_NOT_FOUND_CODES = ("ResourceNotFoundException",)


def _recipes_using_component(client: BaseClient, component_arn: str) -> list[str]:
    """Return Self-owned image-recipe ARNs that reference the component.

    The component build-version ARN ends in a concrete build number
    (``.../component/name/1.0.0/1``); recipes record the ARN WITHOUT the trailing
    build number (``.../component/name/1.0.0``, equal to ``base_arn``). A recipe
    references this component only when its ``componentArn`` is either exactly
    ``base_arn`` (a versionless-build reference to THIS version) or ``base_arn``
    followed by ``"/"`` (a specific build of THIS exact version). Matching on a
    raw-string prefix would over-match sibling versions whose ARN merely shares
    the prefix (``1.0.01``, ``1.0.0-beta``, ``1.0.0/2`` of a *different* base),
    sweeping recipes for other component versions into deletion — so the match is
    boundary-anchored.
    """
    base_arn = component_arn.rsplit("/", 1)[0]
    matching: list[str] = []
    for page in client.get_paginator("list_image_recipes").paginate(owner="Self"):
        for recipe in page.get("imageRecipeSummaryList", []):
            recipe_arn = recipe["arn"]
            detail = client.get_image_recipe(imageRecipeArn=recipe_arn)["imageRecipe"]
            for component in detail.get("components", []):
                referenced = component.get("componentArn", "")
                if referenced == base_arn or referenced.startswith(base_arn + "/"):
                    matching.append(recipe_arn)
                    break
    return matching


def _build_versions_from_recipe(client: BaseClient, recipe_arn: str) -> list[str]:
    """Return build-version ARNs of Self-owned images built from ``recipe_arn``.

    ``list_images``/``list_image_build_versions`` are account-wide and carry no
    recipe link, so each build version is resolved via ``get_image`` and kept only
    when its ``imageRecipe.arn`` matches — never touching images from other recipes.
    """
    matching: list[str] = []
    for page in client.get_paginator("list_images").paginate(owner="Self"):
        for image in page.get("imageVersionList", []):
            for build_page in client.get_paginator("list_image_build_versions").paginate(
                imageVersionArn=image["arn"]
            ):
                for build in build_page.get("imageSummaryList", []):
                    detail = client.get_image(imageBuildVersionArn=build["arn"])["image"]
                    if detail.get("imageRecipe", {}).get("arn") == recipe_arn:
                        matching.append(build["arn"])
    return matching


def _delete_recipe_and_images(client: BaseClient, recipe_arn: str) -> None:
    """Delete the images built from this recipe, then the recipe itself.

    Logs each irreversible deletion so the destroyed ARNs are traceable after the fact
    (over-deletion is the primary risk of the ARN-dependency cascade).
    """
    for build_arn in _build_versions_from_recipe(client, recipe_arn):
        logger.debug(f"Deleting Image Builder image build version {build_arn}")
        client.delete_image(imageBuildVersionArn=build_arn)
    logger.debug(f"Deleting Image Builder recipe {recipe_arn}")
    client.delete_image_recipe(imageRecipeArn=recipe_arn)


@resource_handler("AWS::ImageBuilder::Component", role="prepare")
def _prepare_component(resource: Resource, session: boto3.Session) -> HandlerResult:
    """Delete the recipes/images that reference the component so it can be deleted."""
    client = build_client(session, "imagebuilder")
    try:
        recipes = _recipes_using_component(client, resource.identifier)
        for recipe_arn in recipes:
            _delete_recipe_and_images(client, recipe_arn)
    except (ClientError, BotoCoreError) as e:
        return prepare_error_result(
            e,
            resource,
            not_found_codes=_NOT_FOUND_CODES,
            not_found_message="Component or dependency not found",
            failed_message_prefix="Failed to clear component dependencies",
        )
    return HandlerResult(
        resource_id=resource.identifier,
        resource_type=resource.type,
        action="prepare",
        status=HandlerStatus.SUCCESS,
        message=f"Cleared {len(recipes)} dependent recipe(s)",
    )


@resource_handler("AWS::ImageBuilder::Component", role="delete")
def _delete_component(resource: Resource, session: boto3.Session) -> HandlerResult:
    """Delete the component build version (dependencies cleared by prepare)."""
    return service_delete(
        resource,
        session,
        client_name="imagebuilder",
        op_name="delete_component",
        id_param="componentBuildVersionArn",
        not_found_codes=_NOT_FOUND_CODES,
        already_gone_message="Resource already gone",
        log_label=resource.type,
    )


@resource_handler("AWS::ImageBuilder::Image", role="delete")
def _delete_image(resource: Resource, session: boto3.Session) -> HandlerResult:
    """Delete an image build version via the imagebuilder API."""
    return service_delete(
        resource,
        session,
        client_name="imagebuilder",
        op_name="delete_image",
        id_param="imageBuildVersionArn",
        not_found_codes=_NOT_FOUND_CODES,
        already_gone_message="Resource already gone",
        log_label=resource.type,
    )
