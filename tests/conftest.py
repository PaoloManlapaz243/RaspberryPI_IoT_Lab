from pathlib import Path

#detection_assistant reads the detector's labels from the exported model when it
#is imported. The export is gitignored, so a fresh clone won't have it until
#`python -m iot_lab.ncnn_export` runs; skip those tests instead of erroring.
METADATA = Path(__file__).resolve().parents[1] / "models" / "yolo11n_ncnn_model" / "metadata.yaml"
ASSISTANT_TESTS = ["test_validate_call.py", "test_guard.py", "test_route.py"]

collect_ignore = [] if METADATA.exists() else ASSISTANT_TESTS

def pytest_terminal_summary(terminalreporter):
    #In the summary (not the header) so it shows even with -q
    if not METADATA.exists():
        terminalreporter.write_line(
            f"NOT RUN: {', '.join(ASSISTANT_TESTS)} ({METADATA.name} not found; "
            "run `python -m iot_lab.ncnn_export`)", yellow=True)
