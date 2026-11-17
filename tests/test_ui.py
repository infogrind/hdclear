from rich.progress import Progress

from hdclear import ui


def test_bytes_column_uses_separate_units():
    progress = Progress()
    task_id = progress.add_task("Write", total=1_000_204_886_016, completed=20_500_000_000)
    text = ui.BytesColumn().render(progress.tasks[task_id])
    assert text.plain == "20.5 GB/1.0 TB"


def test_human():
    assert ui.human(0) == "0 B"
    assert ui.human(79_700_000) == "79.7 MB"
    assert ui.human(8_001_563_222_016) == "8.0 TB"
