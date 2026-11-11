import base64
import json
from types import SimpleNamespace

import pytest
import world as world_module
from PIL import Image
from typesafe_sdk import TypeSafeAPIError, TypeSafeAuthenticationError, TypeSafeBadRequestError
from world import FakeTypeSafe, Page, World, drive, scripted

from typesafe_computer_use.decide import IMAGES_HEADER, SCREEN_IMAGE, decide, refuses_the_picture, verify_typed
from typesafe_computer_use.models import Field
from typesafe_computer_use.parse.marks import marks


def answer(choice, confidence=0.9):
    return SimpleNamespace(choice=choice, confidence=confidence, probabilities={choice: confidence})


class Recording:
    """The classifier, keeping every request and failing the first ones with the errors given."""

    def __init__(self, *errors: Exception):
        self.errors = list(errors)
        self.requests: list[dict] = []

    def system_one(self, **request):
        self.requests.append(request)
        if self.errors:
            raise self.errors.pop(0)
        return SimpleNamespace(answers={"kind": answer("click_item"), "item": answer("0"), "site": answer("none", 1.0)})


def refusal(status: int, message: str) -> TypeSafeAPIError:
    kind = TypeSafeBadRequestError if status == 400 else TypeSafeAPIError
    return kind(status, {"error": {"message": message}}, {}, endpoint="POST /v1/system_one")


@pytest.fixture
def scene(screen, make_item):
    items = [make_item(0, "Sign in")]
    return screen, items, marks(screen.image, items, (0, 0, *screen.image.size))


def ask(client, scene, picture=True):
    screen, items, marked = scene
    return decide(client, "sign in", screen, items, [], "Google Chrome", None, picture=marked if picture else None)


def test_the_picture_goes_as_an_asset_the_state_names(scene):
    client = Recording()
    decision = ask(client, scene)

    (request,) = client.requests
    ref = scene[2].ref
    assert request["state"][SCREEN_IMAGE] == ref
    assert request["extra_headers"] == {IMAGES_HEADER: "true"}
    asset = request["extra_body"]["assets"][ref]
    assert asset["type"] == "image" and asset["format"] == "png"
    assert base64.b64decode(asset["data_base64"]) == scene[2].png
    assert "screen_image" in request["questions"]["item"].instructions
    assert ref not in json.dumps({k: v for k, v in request["state"].items() if k != SCREEN_IMAGE})
    assert decision.image_refused is None


def test_every_picture_has_its_own_reference(scene):
    screen, items, _ = scene
    assert (
        marks(screen.image, items, (0, 0, *screen.image.size)).ref != marks(screen.image, items, (0, 0, *screen.image.size)).ref
    )


def test_without_a_picture_the_request_is_as_before(scene):
    client = Recording()
    ask(client, scene, picture=False)

    (request,) = client.requests
    assert set(request) == {"state", "questions"}
    assert SCREEN_IMAGE not in request["state"]
    assert "screen_image" not in request["questions"]["item"].instructions


@pytest.mark.parametrize(
    "error",
    [refusal(400, "images are not enabled for this key"), refusal(400, "asset abc is not a valid PNG"), refusal(413, "")],
)
def test_a_refused_picture_is_dropped_and_the_step_asked_again(scene, error):
    client = Recording(error)
    decision = ask(client, scene)

    first, second = client.requests
    assert "extra_body" in first
    assert set(second) == {"state", "questions"}
    assert SCREEN_IMAGE not in second["state"]
    assert "screen_image" not in second["questions"]["item"].instructions
    assert decision.image_refused == str(error) and decision.clicking


@pytest.mark.parametrize(
    "error",
    [
        refusal(400, "question 'item' has no criteria"),
        TypeSafeAuthenticationError(401, {"error": {"message": "image access needs a valid key"}}, {}),
        refusal(500, "image service down"),
    ],
)
def test_any_other_error_is_raised_without_a_second_request(scene, error):
    client = Recording(error)
    with pytest.raises(TypeSafeAPIError):
        ask(client, scene)
    assert len(client.requests) == 1
    assert not refuses_the_picture(error)


def test_a_second_failure_is_raised(scene):
    client = Recording(refusal(413, ""), refusal(400, "question 'kind' is malformed"))
    with pytest.raises(TypeSafeBadRequestError):
        ask(client, scene)
    assert len(client.requests) == 2


def test_the_typing_check_sends_no_picture():
    client = SimpleNamespace(requests=[])

    def system_one(**request):
        client.requests.append(request)
        return SimpleNamespace(answers={"ok": SimpleNamespace(noul=0.9)})

    client.system_one = system_one
    field = Field(role="AXTextField", label="Email", placeholder="", value="", x=0, y=0, w=10, h=10)
    verify_typed(client, "sign in", field, "a@b.c", field)

    (request,) = client.requests
    assert set(request) == {"state", "questions"}
    assert SCREEN_IMAGE not in request["state"]


def test_each_step_saves_the_picture_it_sent_and_names_it_in_the_payload(monkeypatch, tmp_path):
    world = World([Page(name="done", items=["Order 4821"], url="https://example.com/done")])
    drive(world, scripted(("done", None)), monkeypatch=monkeypatch, tmp_path=tmp_path)

    folder = tmp_path / "run"
    (extra,) = world.fake.extras
    (ref,) = extra["extra_body"]["assets"]
    sent = (folder / "step-001-sent.png").read_bytes()
    assert base64.b64decode(extra["extra_body"]["assets"][ref]["data_base64"]) == sent
    assert world.fake.states[0][SCREEN_IMAGE] == ref
    with Image.open(folder / "step-001-sent.png") as image:
        width, height = image.size
    payload = (folder / "step-001-payload.txt").read_text()
    assert f"IMAGE  {SCREEN_IMAGE}={ref}  {width}x{height} PNG" in payload
    assert extra["extra_body"]["assets"][ref]["data_base64"][:40] not in payload
    run = json.loads((folder / "run.json").read_text())
    assert run["image_refusals"] == []
    assert run["perception"]["pipeline"] == "omniparser-1"
    assert run["perception"]["weights"]["microsoft/OmniParser-v2.0"]["sha256"]


def test_a_refused_picture_is_logged_and_recorded_in_the_run(monkeypatch, tmp_path):
    class Refusing(FakeTypeSafe):
        def system_one(self, state, questions, **extra):
            if extra:
                raise refusal(413, "request entity too large")
            return super().system_one(state, questions)

    monkeypatch.setattr(world_module, "FakeTypeSafe", Refusing)
    world = World([Page(name="done", items=["Order 4821"], url="https://example.com/done")])
    drive(world, scripted(("done", None)), monkeypatch=monkeypatch, tmp_path=tmp_path)

    folder = tmp_path / "run"
    run = json.loads((folder / "run.json").read_text())
    (refused,) = run["image_refusals"]
    assert refused["step"] == 1 and "413" in refused["reason"]
    assert "the screen picture was refused" in (folder / "run.log").read_text()
    assert SCREEN_IMAGE not in world.fake.states[0]
