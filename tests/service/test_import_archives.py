import asyncio
import stat
import zipfile
from unittest.mock import patch

import pytest

from service.db.archive_input import safe_extract
from service.db.input_rows import prepare_input, source_date
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


@pytest.mark.parametrize("with_notice", [False, True])
def test_archive_import(database, tmp_path, with_notice):
    async def run():
        day = write_day(tmp_path)
        path = tmp_path / "2026-11-14.zip"
        with zipfile.ZipFile(path, "w") as archive:
            if with_notice:
                archive.writestr("archive-info.txt", "Archive notice")
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


@pytest.mark.parametrize(
    "notice_kind", ["symlink", "directory", "unexpected", "oversize"]
)
def test_invalid_root_notice(tmp_path, notice_kind):
    day = write_day(tmp_path)
    notice = day / "archive-info.txt"
    if notice_kind == "symlink":
        notice.symlink_to(day / "test" / "stores.csv")
    elif notice_kind == "directory":
        notice.mkdir()
    elif notice_kind == "unexpected":
        (day / "unexpected.txt").write_text("Unexpected root file")
    else:
        notice.write_text("Notice")
    destination = tmp_path / "snapshot"
    destination.mkdir()
    with patch(
        "service.db.input_rows.MAX_FILE_BYTES",
        3 if notice_kind == "oversize" else 2 * 1024**3,
    ):
        with pytest.raises(ValueError):
            prepare_input(day, destination)
