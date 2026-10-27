from orchestra_bridges.identity import People


def test_an_account_not_listed_is_known_by_its_login():
    a = People().actor("github", "SomeOne")
    assert (a.source, a.login, a.bot, a.name, a.team) == ("person.someone", "someone", False, None, None)


def test_bots_are_told_by_the_system_or_the_bot_suffix():
    assert People().actor("github", "github-actions[bot]").source == "bot.github-actions"
    assert People().actor("github", "vscode-triage-bot", system_says_bot=True).source == "bot.vscode-triage-bot"


def test_a_listed_account_is_its_person_with_name_and_team(tmp_path):
    f = tmp_path / "people.toml"
    f.write_text('[people.daniel]\nname = "Daniel Lewis"\nteam = "sre"\ngithub = "DanRobLewis"\nslack = "U123"\n'
                 '[people.triage]\nbot = true\nteam = "vscode"\ngithub = ["vscode-triage-bot", "vs-code-engineering"]\n')
    people = People.load(f)
    a = people.actor("github", "danroblewis")
    assert (a.source, a.name, a.team) == ("person.daniel", "Daniel Lewis", "sre")
    assert people.actor("slack", "u123").source == "person.daniel"          # one person across systems
    assert people.actor("github", "vs-code-engineering").source == "bot.triage"
    assert a.announcement("github") == {"kind": "person", "mode": "ad hoc", "package": "bridges", "github": "danroblewis",
                                        "bot": False, "name": "Daniel Lewis", "team": "sre"}
    assert people.actor("github", "vscode-triage-bot").announcement("github")["kind"] == "program"


def test_a_deleted_account_is_ghost_and_odd_characters_are_made_safe():
    assert People().actor("github", None).source == "person.ghost"
    assert People().actor("github", "we/ird name").source == "person.we-ird-name"


def test_no_map_file_lists_nobody(tmp_path):
    assert People.load(tmp_path / "missing.toml").actor("github", "x").source == "person.x"
    assert People.load(None).actor("github", "x").source == "person.x"
