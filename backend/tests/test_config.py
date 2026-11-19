"""Loading the config: a key it doesn't know stops the load, with a message that names the file and the
setting. Every config here is made up and lives in the test's own folder."""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import pytest
import yaml
from pydantic import BaseModel

from switchboard import desktop
from switchboard.__main__ import build_parser, main
from switchboard.config import REPO_ROOT, Config, ConfigError, load_config

BASE = {
    "models": {"qwen-moe": {"provider": "ollama", "model": "qwen3.6:35b-a3b"}},
    "routing": {"classifier": {"model": "qwen-moe"}, "rules": [{"name": "default", "model": "qwen-moe"}]},
    "pricing": {"reference_model": None},
}


def write(tmp_path: Path, extra: dict | None = None, local: dict | str | None = None) -> Path:
    """A config.yaml in the test's folder with `extra` added, and a config.local.yaml next to it
    when `local` is given."""
    path = tmp_path / "config.yaml"
    raw = {**BASE, "server": {"data_dir": str(tmp_path / "data")},
           "claude": {"logs_dir": str(tmp_path / "claude-home")}, **(extra or {})}
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    if local is not None:
        text = local if isinstance(local, str) else yaml.safe_dump(local)
        (tmp_path / "config.local.yaml").write_text(text, encoding="utf-8")
    return path


def problem(path: Path) -> str:
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    return str(caught.value)


def test_an_unknown_top_level_key_stops_the_load(tmp_path):
    assert problem(write(tmp_path, {"colour": "teal"})) == "In config.yaml, colour isn't a setting at the top level."


def test_an_unknown_nested_key_is_named_by_its_path(tmp_path):
    path = write(tmp_path, {"brief": {"mail": {"cliend_id": "made-up-id"}}})
    assert problem(path) == "In config.yaml, brief.mail.cliend_id isn't a setting."


def test_a_section_in_the_wrong_place_of_the_local_file_says_where_it_goes(tmp_path):
    # mail one level too far out, so it sits beside brief instead of under it
    path = write(tmp_path, local="brief:\n  enabled: true\nmail:\n  client_id: made-up-id\n")
    assert problem(path) == ("In config.local.yaml, mail isn't a setting at the top level. "
                             "It goes under brief, as brief.mail.")


@pytest.mark.parametrize(("local", "hint"), [
    ({"brief": {"client_id": "made-up-id"}}, "It goes under brief.mail, as brief.mail.client_id."),
    ({"effort": {"by_reasoning": {1: "low"}}}, "It goes under routing, as routing.effort."),
    ({"enabled": True}, "It goes under index, brief or routing.learn."),
    ({"brief": {"models": {}}}, "It goes at the top level or under pricing."),
    ({"max_effort": "high"}, "It goes under models.<name> or routing.rules[n]."),
])
def test_a_misplaced_setting_is_pointed_to_where_it_belongs(tmp_path, local, hint):
    assert problem(write(tmp_path, local=local)).endswith(hint)


def test_only_a_setting_that_takes_that_kind_of_value_is_suggested(tmp_path):
    # chat.memory is a section, not a switch, so memory: true isn't sent there
    assert problem(write(tmp_path, local={"memory": True})) == ("In config.local.yaml, memory isn't a setting "
                                                                "at the top level.")


def test_several_problems_are_listed_under_their_file(tmp_path):
    path = write(tmp_path, {"colour": "teal"},
                 local={"mail": {"tenant": "consumers"}, "brief": {"mail": {"max_messages": 0}}})
    text = problem(path)
    assert text.startswith("In config.local.yaml:\n  - brief.mail.max_messages: Input should be greater than")
    assert text.endswith("\n  - mail isn't a setting at the top level. It goes under brief, as brief.mail.\n"
                         "In config.yaml:\n  - colour isn't a setting at the top level.")


def test_a_validator_s_own_words_follow_the_setting(tmp_path):
    path = write(tmp_path, local={"brief": {"build_after": "noon"}})
    assert problem(path) == 'In config.local.yaml, brief.build_after has to be a time of day, such as "06:30"'


def test_a_missing_setting_is_put_in_the_file_that_has_its_section(tmp_path):
    path = write(tmp_path, local={"models": {"qwen-small": {"provider": "ollama"}}})
    assert problem(path) == "In config.local.yaml, models.qwen-small.model is missing."


def test_a_check_across_settings_names_no_file(tmp_path):
    # the rules come from the local file here, so naming config.yaml would send you to the wrong one
    path = write(tmp_path, local={"routing": {"rules": [{"name": "mine", "model": "nope"}]}})
    assert problem(path) == "Problems in the config:\n  - rule 'mine' uses unknown model 'nope'"


def test_the_tracked_config_has_no_stray_keys():
    # Validated directly: load_config would lay whatever config.local.yaml sits next to it on top.
    Config.model_validate(yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8")))


def models_in(annotation):
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
    for arg in get_args(annotation):
        yield from models_in(arg)


def test_every_part_of_the_config_refuses_keys_it_does_not_know():
    """A section added later has to be a Settings too, or a key misspelled in it is dropped without a word."""
    seen, todo = set(), [Config]
    while todo:
        model = todo.pop()
        if model not in seen:
            seen.add(model)
            todo += [inner for field in model.model_fields.values() for inner in models_in(field.annotation)]
    assert len(seen) > 15  # the walk reached the nested sections
    assert [model.__name__ for model in seen if model.model_config.get("extra") != "forbid"] == []


# How the owner hears about it ------------------------------------------------------


def test_a_command_prints_a_bad_config_without_a_traceback(tmp_path, monkeypatch, capsys):
    path = write(tmp_path, local={"mail": {"tenant": "consumers"}})
    monkeypatch.setattr("switchboard.config.get_config", lambda: load_config(path))
    assert main(["sync"]) == 1
    err = capsys.readouterr().err
    assert err == ("Switchboard's config didn't load.\n\n"
                   "In config.local.yaml, mail isn't a setting at the top level. It goes under brief, as brief.mail.\n")


def test_the_app_says_it_in_a_dialog(tmp_path, monkeypatch):
    path = write(tmp_path, local={"mail": {"tenant": "consumers"}})
    alerts = []
    monkeypatch.setattr(desktop, "get_config", lambda: load_config(path))
    monkeypatch.setattr(desktop, "alert", alerts.append)
    # Had the config loaded, the app would go on to claim the port, where a real Switchboard may be running.
    monkeypatch.setattr(desktop, "setup_logging", lambda file: None)
    monkeypatch.setattr(desktop, "_run", lambda cfg, args: pytest.fail("the app went on without a config"))
    assert desktop.run(build_parser().parse_args(["app"])) == 1
    assert alerts == ["Switchboard can't start because its config didn't load.\n\n"
                      "In config.local.yaml, mail isn't a setting at the top level. "
                      "It goes under brief, as brief.mail."]
