"""Tests for the EC2 Image Builder cleanup handlers."""

from __future__ import annotations

from unittest.mock import MagicMock

from botocore.exceptions import ClientError

from aws_bench.resource_management.ccapi.models import Resource
from aws_bench.resource_management.cleanup.handlers.imagebuilder import (
    _build_versions_from_recipe,
    _delete_component,
    _delete_image,
    _prepare_component,
    _recipes_using_component,
)
from aws_bench.resource_management.cleanup.models import HandlerStatus

_COMPONENT = "arn:aws:imagebuilder:us-east-1:111122223333:component/install-awscli-v2/1.0.0/1"
_RECIPE = "arn:aws:imagebuilder:us-east-1:111122223333:image-recipe/al2-awscli-v2-recipe/1.0.0"
_IMAGE = "arn:aws:imagebuilder:us-east-1:111122223333:image/al2-awscli-v2-recipe/1.0.0/1"
_OTHER_IMAGE = "arn:aws:imagebuilder:us-east-1:111122223333:image/unrelated/1.0.0/1"
_OTHER_RECIPE = "arn:aws:imagebuilder:us-east-1:111122223333:image-recipe/unrelated/1.0.0"


def _paginator(pages: list[dict]) -> MagicMock:
    p = MagicMock()
    p.paginate.return_value = pages
    return p


def _client_with_paginators(pages_by_op: dict[str, list[dict]]) -> MagicMock:
    """Build a client whose ``get_paginator(op)`` returns a fake with those pages."""
    client = MagicMock()
    paginators = {op: _paginator(pages) for op, pages in pages_by_op.items()}
    client.get_paginator.side_effect = paginators.__getitem__
    return client


# -- _recipes_using_component --


def test_recipes_using_component_matches_on_version_prefix():
    other_recipe = "arn:.../other-recipe/1.0.0"
    client = _client_with_paginators(
        {
            "list_image_recipes": [
                {"imageRecipeSummaryList": [{"arn": _RECIPE}, {"arn": other_recipe}]}
            ]
        }
    )
    # The recipe records the component WITHOUT the trailing build number.
    base = _COMPONENT.rsplit("/", 1)[0]
    client.get_image_recipe.side_effect = [
        {"imageRecipe": {"components": [{"componentArn": base}]}},
        {"imageRecipe": {"components": [{"componentArn": "arn:.../unrelated/1.0.0"}]}},
    ]
    result = _recipes_using_component(client, _COMPONENT)
    assert result == [_RECIPE]


def test_recipes_using_component_is_boundary_anchored():
    """A recipe referencing a SIBLING version must not be swept in.

    ``base_arn`` is ``.../component/install-awscli-v2/1.0.0``. Matching on a raw
    string prefix would also match ``1.0.01`` / ``1.0.0-beta`` / a longer sibling
    ARN. Only an exact ``base_arn`` or ``base_arn + "/"`` (a build of THIS version)
    must match.
    """
    base = _COMPONENT.rsplit("/", 1)[0]
    recipe_exact = "arn:aws:imagebuilder:us-east-1:111122223333:image-recipe/exact/1.0.0"
    recipe_build = "arn:aws:imagebuilder:us-east-1:111122223333:image-recipe/build/1.0.0"
    recipe_sibling = "arn:aws:imagebuilder:us-east-1:111122223333:image-recipe/sibling/1.0.0"
    recipe_beta = "arn:aws:imagebuilder:us-east-1:111122223333:image-recipe/beta/1.0.0"
    client = _client_with_paginators(
        {
            "list_image_recipes": [
                {
                    "imageRecipeSummaryList": [
                        {"arn": recipe_exact},
                        {"arn": recipe_build},
                        {"arn": recipe_sibling},
                        {"arn": recipe_beta},
                    ]
                }
            ]
        }
    )
    client.get_image_recipe.side_effect = [
        # Exact base_arn -> matches (versionless-build reference).
        {"imageRecipe": {"components": [{"componentArn": base}]}},
        # base_arn + "/2" -> matches (a specific build of THIS exact version).
        {"imageRecipe": {"components": [{"componentArn": base + "/2"}]}},
        # "1.0.01" shares the string prefix but is a DIFFERENT version -> no match.
        {"imageRecipe": {"components": [{"componentArn": base + "1"}]}},
        # "1.0.0-beta" shares the string prefix but is a DIFFERENT version -> no match.
        {"imageRecipe": {"components": [{"componentArn": base + "-beta"}]}},
    ]
    result = _recipes_using_component(client, _COMPONENT)
    assert result == [recipe_exact, recipe_build]


# -- _build_versions_from_recipe (scopes deletion to one recipe) --


def test_build_versions_from_recipe_only_returns_matching_recipe():
    """Images built from OTHER recipes must not be selected for deletion."""
    client = _client_with_paginators(
        {
            # One image version listing two build versions; get_image (below) is
            # what scopes the result to _RECIPE.
            "list_images": [{"imageVersionList": [{"arn": _IMAGE}]}],
            "list_image_build_versions": [
                {"imageSummaryList": [{"arn": _IMAGE}, {"arn": _OTHER_IMAGE}]}
            ],
        }
    )
    # Build version resolves back to its source recipe; only _IMAGE's matches _RECIPE.
    client.get_image.side_effect = lambda imageBuildVersionArn: {
        "image": {
            "imageRecipe": {"arn": _RECIPE if imageBuildVersionArn == _IMAGE else _OTHER_RECIPE}
        }
    }

    assert _build_versions_from_recipe(client, _RECIPE) == [_IMAGE]


# -- _prepare_component --


def test_prepare_component_deletes_dependent_recipes_and_images():
    session = MagicMock()
    client = _client_with_paginators(
        {
            "list_image_recipes": [{"imageRecipeSummaryList": [{"arn": _RECIPE}]}],
            "list_images": [{"imageVersionList": [{"arn": _IMAGE}]}],
            "list_image_build_versions": [{"imageSummaryList": [{"arn": _IMAGE}]}],
        }
    )
    session.client.return_value = client
    base = _COMPONENT.rsplit("/", 1)[0]
    client.get_image_recipe.return_value = {"imageRecipe": {"components": [{"componentArn": base}]}}
    # The build version resolves back to _RECIPE, so it is in scope for deletion.
    client.get_image.return_value = {"image": {"imageRecipe": {"arn": _RECIPE}}}

    result = _prepare_component(
        Resource(type="AWS::ImageBuilder::Component", identifier=_COMPONENT), session
    )
    client.delete_image.assert_called_once_with(imageBuildVersionArn=_IMAGE)
    client.delete_image_recipe.assert_called_once_with(imageRecipeArn=_RECIPE)
    assert result.status == HandlerStatus.SUCCESS
    assert "1 dependent recipe" in result.message


def test_prepare_component_no_dependents_succeeds():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.get_paginator.return_value = _paginator([{"imageRecipeSummaryList": []}])
    result = _prepare_component(
        Resource(type="AWS::ImageBuilder::Component", identifier=_COMPONENT), session
    )
    client.delete_image_recipe.assert_not_called()
    assert result.status == HandlerStatus.SUCCESS


def test_prepare_component_skips_when_not_found():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.get_paginator.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException"}}, "ListImageRecipes"
    )
    result = _prepare_component(
        Resource(type="AWS::ImageBuilder::Component", identifier=_COMPONENT), session
    )
    assert result.status == HandlerStatus.SKIPPED


# -- _delete_component / _delete_image --


def test_delete_component_success():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    result = _delete_component(
        Resource(type="AWS::ImageBuilder::Component", identifier=_COMPONENT), session
    )
    client.delete_component.assert_called_once_with(componentBuildVersionArn=_COMPONENT)
    assert result.status == HandlerStatus.SUCCESS


def test_delete_image_already_gone_is_success():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.delete_image.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException"}}, "DeleteImage"
    )
    result = _delete_image(Resource(type="AWS::ImageBuilder::Image", identifier=_IMAGE), session)
    assert result.status == HandlerStatus.SUCCESS


def test_delete_image_failure_on_other_error():
    session = MagicMock()
    client = MagicMock()
    session.client.return_value = client
    client.delete_image.side_effect = ClientError(
        {"Error": {"Code": "InvalidRequestException"}}, "DeleteImage"
    )
    result = _delete_image(Resource(type="AWS::ImageBuilder::Image", identifier=_IMAGE), session)
    assert result.status == HandlerStatus.FAILED
