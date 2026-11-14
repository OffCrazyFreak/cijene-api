import asyncio
import stat
import zipfile
from unittest.mock import patch

import pytest

from service.db.archive_input import safe_extract
from service.db.input_rows import source_date
from tests.service.import_fixtures import importer, write_day


@pytest.mark.parametrize(
    "name", ["../escape", "/absolute", "a/../../escape", "a\\escape", "a/b/c.csv"]
)
def test_unsafe_member(tmp_path, name):
    path = tmp_path / "2026-11-14.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(name, "bad")
    with pytest.raises(ValueError):
        safe_extract(path, tmp_path / "out")


@pytest.mark.parametrize("attack", ["duplicate", "symlink", "expansion", "count"])
def test_archive_limits(tmp_path, attack):
    path = tmp_path / "2026-11-14.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if attack == "symlink":
            member = zipfile.ZipInfo("test/prices.csv")
            member.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(member, "/tmp/target")
        elif attack == "expansion":
            archive.writestr("test/prices.csv", "0" * 2000000)
        else:
            for number in range(501 if attack == "count" else 2):
                archive.writestr(
                    f"test/{number if attack == 'count' else 'duplicate'}", "x"
                )
    with pytest.raises(ValueError):
        safe_extract(path, tmp_path / "out")


def test_archive_import(database, tmp_path):
    async def run():
        day = write_day(tmp_path)
        path = tmp_path / "2026-11-14.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for file in day.rglob("*.csv"):
                archive.write(file, file.relative_to(day))
        async with database() as db:
            await db.create_tables()
            with patch.object(importer, "db", db):
                await importer.import_archive(path)
            assert await db._fetchval("SELECT count(*) FROM prices") == 1

    asyncio.run(run())


@pytest.mark.parametrize("value", ["2026-02-30", "2026-1-01", "bad", "2026-11-14extra"])
def test_bad_source_date(value):
    with pytest.raises(ValueError):
        source_date(value)
