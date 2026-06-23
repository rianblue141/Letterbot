from letterboxd_bot.bot import LetterboxdCog


def test_discord_command_module_imports_with_expected_groups() -> None:
    assert LetterboxdCog.summary_group.name == "summary"
    assert LetterboxdCog.compare_group.name == "compare"
    assert {command.name for command in LetterboxdCog.summary_group.commands} == {
        "all",
        "all-time",
        "user",
    }
    assert {command.name for command in LetterboxdCog.compare_group.commands} == {
        "all-time",
        "rss",
    }
    assert LetterboxdCog.ping.name == "ping"
    assert LetterboxdCog.import_letterboxd.name == "import-letterboxd"
