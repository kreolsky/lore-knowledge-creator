"""Queue-name safety — shared constants and startup validation.

# WHY: arq silently drops jobs when the enqueue queue_name != worker queue_name.
# A shared constant + startup assert prevents the "enqueued but never consumed" failure
# from reaching production. Plan §P2-#4.
"""

import inspect

import pytest


class TestQueueNameSafety:
    def test_transcription_worker_queue_matches_constant(self):
        """TranscriptionWorkerSettings.queue_name must equal the shared constant."""
        from jobs.pool import TRANSCRIPTION_QUEUE
        from jobs.worker import TranscriptionWorkerSettings

        assert TranscriptionWorkerSettings.queue_name == TRANSCRIPTION_QUEUE

    def test_enqueue_transcription_uses_constant(self):
        """enqueue_transcription must pass TRANSCRIPTION_QUEUE, not a bare string."""
        import transcription

        from jobs.pool import TRANSCRIPTION_QUEUE

        source = inspect.getsource(transcription.enqueue_transcription)
        assert TRANSCRIPTION_QUEUE in source, (
            f"enqueue_transcription must reference TRANSCRIPTION_QUEUE ({TRANSCRIPTION_QUEUE!r}), "
            f"not a bare string literal"
        )

    def test_recovery_uses_constant(self):
        """_recover_stuck_transcriptions must pass TRANSCRIPTION_QUEUE, not a bare string."""
        import main
        from jobs.pool import TRANSCRIPTION_QUEUE

        source = inspect.getsource(main._recover_stuck_transcriptions)
        assert TRANSCRIPTION_QUEUE in source, (
            f"_recover_stuck_transcriptions must reference TRANSCRIPTION_QUEUE ({TRANSCRIPTION_QUEUE!r})"
        )


    def test_validate_queue_config_succeeds(self):
        """validate_queue_config() must not raise when config is consistent."""
        from jobs.worker import validate_queue_config

        validate_queue_config()

    def test_validate_queue_config_catches_mismatch(self, monkeypatch):
        """validate_queue_config() must raise AssertionError on queue_name mismatch."""
        from jobs.worker import TranscriptionWorkerSettings, validate_queue_config

        monkeypatch.setattr(TranscriptionWorkerSettings, "queue_name", "wrong_queue")
        with pytest.raises(AssertionError, match="queue_name"):
            validate_queue_config()


    def test_validate_queue_config_catches_queue_collision(self, monkeypatch):
        """validate_queue_config() must raise when both workers bind the same queue."""
        from jobs.pool import TRANSCRIPTION_QUEUE
        from jobs.worker import WorkerSettings, validate_queue_config

        monkeypatch.setattr(WorkerSettings, "queue_name", TRANSCRIPTION_QUEUE, raising=False)
        with pytest.raises(AssertionError, match="same queue"):
            validate_queue_config()

    def test_validate_queue_config_catches_function_overlap(self, monkeypatch):
        """validate_queue_config() must raise AssertionError when functions overlap."""
        from jobs.worker import (
            TranscriptionWorkerSettings,
            WorkerSettings,
            validate_queue_config,
        )

        original_trans = list(TranscriptionWorkerSettings.functions)
        monkeypatch.setattr(
            TranscriptionWorkerSettings,
            "functions",
            original_trans + [WorkerSettings.functions[0]],
        )
        with pytest.raises(AssertionError, match="registered in both"):
            validate_queue_config()


    def test_validate_queue_config_catches_missing_backup_task(self, monkeypatch):
        """validate_queue_config() must crash if a backup task is declared but not
        registered in WorkerSettings.functions — otherwise it would enqueue but never run."""
        from jobs.worker import WorkerSettings, validate_queue_config

        # Drop one backup task from the registered functions → the completeness guard
        # must fire.
        pruned = [f for f in WorkerSettings.functions if getattr(f, "name", None) != "auto_backup_loss_task"]
        monkeypatch.setattr(WorkerSettings, "functions", pruned)
        with pytest.raises(AssertionError, match="declared but not registered"):
            validate_queue_config()


    def test_validate_queue_config_catches_missing_comfy_task(self, monkeypatch):
        """validate_queue_config() must crash if a Comfy image-gen task is declared but
        not registered in WorkerSettings.functions — otherwise the launcher enqueues a
        generation arq silently drops, leaving a spinner that never clears (Bug 1)."""
        from jobs.worker import WorkerSettings, validate_queue_config

        pruned = [
            f for f in WorkerSettings.functions
            if getattr(f, "name", None) != "generate_image_task"
        ]
        monkeypatch.setattr(WorkerSettings, "functions", pruned)
        with pytest.raises(AssertionError, match="Comfy image-gen tasks"):
            validate_queue_config()
