from rea_bot import config


def test_saved_config_and_database_are_independent_of_working_directory(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text(
        "BOT_TOKEN=example-test-token\nSUPERADMIN_IDS=12345\nDATABASE_PATH=data/bot.sqlite3\n"
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / ".env").write_text("BOT_TOKEN=wrong-copy\nSUPERADMIN_IDS=999\n")
    for key in ("BOT_TOKEN", "SUPERADMIN_IDS", "DATABASE_PATH", "REA_ENV_FILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config, "PROJECT_DIR", project)
    monkeypatch.chdir(elsewhere)
    saved = (project / ".env").read_text()
    for _ in range(2):
        settings = config.Settings.from_env()
        assert settings.token == "example-test-token"
        assert settings.admins == frozenset({12345})
        assert settings.database == str(project / "data/bot.sqlite3")
    assert (project / ".env").read_text() == saved
