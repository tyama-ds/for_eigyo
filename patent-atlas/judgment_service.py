"""Shared patent judgment services: LLM assessment and model training.

Both run synchronously on the caller's thread; the caller owns ``ws.job`` and
``ws.stop`` (normally through ``job_runner.begin_job``). ``complete``,
``train``, ``save`` and ``progress`` are injectable so the application keeps
its replaceable seams.
"""

import copy

import llm_judgment
from analysis_engine import train as default_train
from job_runner import progress as default_progress
from llm import complete as default_complete

LLM_CHECKPOINT_BATCHES = 10


def assessment_options(ws, body, *, allow_empty=False):
    """Snapshot only the applied query; unsaved workbench plans are not criteria."""
    workbench = ws.state.get("research_workbench") or {}
    active_id = workbench.get("active_query_id")
    adopted = next((query for query in ws.state["queries"] if active_id and query.get("id") == active_id), None)
    return llm_judgment.prepare(
        body,
        ws.state["patents"],
        ws.state["keywords"],
        ws.settings["provider"],
        allow_empty=allow_empty,
        adopted_query=copy.deepcopy(adopted),
    )


class JudgmentCheckpoint:
    """Durable checkpoints of one LLM judgment run.

    ``remember`` keeps one undo image per changed row since the last durable
    save, ``publish`` applies a batch and saves at the configured cadence, and
    ``rollback`` restores rows, training metadata, orchestration and job
    progress to the last checkpoint when a save fails. Construct under the lock.
    """

    COUNT_KEYS = ("completed_count", "judged_count", "deferred_count", "protected_count", "progress")

    def __init__(self, ws, options, settings_snapshot, *, save, report_progress, checkpoint_batches):
        self.ws = ws
        self.options = options
        self.settings = settings_snapshot
        self.save = save
        self.report_progress = report_progress
        self.checkpoint_batches = checkpoint_batches
        self.training = copy.deepcopy(ws.state["training"])
        self.orchestration = copy.deepcopy(ws.state.get("orchestration"))
        self.progress = {key: ws.job[key] for key in ("progress", "message")}
        self.initialized = False
        self.pending_rows = {}
        self.pending_batches = 0
        self.saved_metadata = None
        self.failed = False

    def remember(self, row):
        # Keep one undo snapshot per changed row since the last durable checkpoint.
        if id(row) not in self.pending_rows:
            self.pending_rows[id(row)] = (row, copy.deepcopy(row))

    def _reset_scores(self):
        """First publish of a running job clears scores not produced by the same judgment."""
        state = self.ws.state
        prior = state.get("training") or {}
        same_judgment = (
            prior.get("mode") == "llm"
            and prior.get("criteria") == self.options["criteria"]
            and prior.get("provider") == self.settings.get("provider")
            and prior.get("model") == self.settings.get("model")
            and prior.get("context_fingerprint") == self.options.get("context_fingerprint")
        )
        for row in state["patents"]:
            if (
                same_judgment
                and row.get("label") in ("keep", "exclude")
                and row.get("label_source") == "agent"
                and row.get("score_source") == "llm"
            ):
                continue
            self.remember(row)
            row["score"] = None
            row["score_source"] = None

    def _apply_updates(self, updates, metadata):
        """Write judged rows, protecting human labels; a stop discards the batch."""
        state = self.ws.state
        current = {row["id"]: row for row in state["patents"]} if updates else {}
        if updates and self.ws.stop.is_set():
            previous = state.get("training") or {}
            for key in self.COUNT_KEYS[:4] + ("label_counts", "progress"):
                if key in previous:
                    metadata[key] = copy.deepcopy(previous[key])
            metadata.update(
                status="cancelled",
                stage="停止",
                message=f"停止 {metadata['completed_count']}/{metadata['total_count']}",
            )
            updates = []
        for update in updates:
            row = current.get(update["id"])
            if row is None or row.get("label_source") == "human" or row.get("label"):
                metadata["protected_count"] += 1
                if update["label"]:
                    metadata["judged_count"] -= 1
                    metadata["label_counts"][update["label"]] -= 1
                else:
                    metadata["deferred_count"] -= 1
                continue
            self.remember(row)
            row.update({key: value for key, value in update.items() if key != "id"})
        return updates

    def _restore_counts(self, metadata):
        # llm_judgment may have accepted several in-memory batches.
        # A failed checkpoint rolls ALL of those counts back too.
        saved = self.saved_metadata or {}
        for key in self.COUNT_KEYS:
            metadata[key] = saved.get(key, 0)
        metadata["label_counts"] = copy.deepcopy(saved.get("label_counts", {"keep": 0, "exclude": 0}))
        metadata["message"] = (
            f"{metadata['stage']} {metadata['completed_count']}/{metadata['total_count']}。"
            "保存に失敗したため、最後の保存以降の未保存判定は取り消しました。"
        )

    def publish(self, updates, metadata):
        ws, state = self.ws, self.ws.state
        with ws.lock:
            previous_status = (state.get("training") or {}).get("status")
            first_publish = not self.initialized
            temp_path = ws.data_dir / "workspace.tmp"
            attempted_save = False
            previous_temp = None
            try:
                if not self.initialized:
                    # An error-only report after failed initialization must
                    # preserve the scores that existed before this job.
                    if metadata["status"] == "running":
                        self._reset_scores()
                    self.initialized = True
                updates = self._apply_updates(updates, metadata)
                if self.failed:
                    self._restore_counts(metadata)
                self.pending_batches += bool(updates)
                terminal = metadata["status"] in ("completed", "cancelled", "error")
                should_save = (
                    first_publish
                    or terminal
                    or metadata["status"] != previous_status
                    or self.pending_batches >= self.checkpoint_batches
                )
                metadata["saved_count"] = (
                    metadata["completed_count"] if should_save else (self.saved_metadata or {}).get("completed_count", 0)
                )
                if not terminal:
                    metadata["message"] += (
                        f"（保存済み {metadata['saved_count']}件。保存失敗・アプリ終了時は、"
                        "最後の保存以降の判定が失われます。）"
                    )
                metadata.update(
                    job_id=ws.job["id"], provider=self.settings["provider"], model=self.settings.get("model", "")
                )
                state["training"] = metadata
                ws.invalidate_public_state()
                self.report_progress(metadata["progress"], metadata["message"])
                if should_save:
                    previous_temp = temp_path.read_bytes() if temp_path.exists() else None
                    attempted_save = True
                    self.save()
                    self.training = copy.deepcopy(metadata)
                    self.orchestration = copy.deepcopy(state.get("orchestration"))
                    self.progress = {key: ws.job[key] for key in ("progress", "message")}
                    self.saved_metadata = copy.deepcopy(metadata)
                    self.pending_rows.clear()
                    self.pending_batches = 0
                    self.failed = False
            except Exception:
                self.rollback(temp_path, attempted_save, previous_temp)
                raise ValueError(
                    "LLM判定の保存に失敗しました。未保存のバッチを取り消し、直前に保存できた判定を保持しています。保存先の空き容量と書き込み権限を確認してください。"
                ) from None
            return copy.deepcopy(metadata)

    def rollback(self, temp_path, attempted_save, previous_temp):
        """Restore the last durable checkpoint in memory and on the temp file; called under the lock."""
        ws, state = self.ws, self.ws.state
        for row, snapshot in self.pending_rows.values():
            if row.get("label_source") == "human" and snapshot.get("label_source") != "human":
                continue
            row.clear()
            row.update(snapshot)
        self.pending_rows.clear()
        self.pending_batches = 0
        self.failed = True
        state["training"] = copy.deepcopy(self.training)
        if isinstance(self.orchestration, dict) and isinstance(state.get("orchestration"), dict):
            state["orchestration"].clear()
            state["orchestration"].update(copy.deepcopy(self.orchestration))
        elif self.orchestration is not None:
            state["orchestration"] = copy.deepcopy(self.orchestration)
        ws.job.update(self.progress)
        ws.invalidate_public_state()
        self.initialized = self.saved_metadata is not None
        try:
            if not attempted_save:
                pass
            elif previous_temp is None:
                temp_path.unlink(missing_ok=True)
            else:
                temp_path.write_bytes(previous_temp)
        except OSError:
            raise ValueError(
                "LLM判定の保存に失敗し、一時ファイルも復元できませんでした。保存先の空き容量と書き込み権限を確認してください。"
            ) from None


def run_llm_assessment(
    ws,
    body,
    progress_callback=None,
    *,
    _prepared=None,
    complete=default_complete,
    save=None,
    progress=None,
    checkpoint_batches=None,
):
    """Synchronous shared judgment service; the caller owns JOB and STOP."""
    save = ws.save if save is None else save
    checkpoint_batches = LLM_CHECKPOINT_BATCHES if checkpoint_batches is None else checkpoint_batches
    report_progress = progress_callback or progress or (lambda value, message: default_progress(ws, value, message))
    with ws.lock:
        if _prepared is None:
            options = assessment_options(ws, body, allow_empty=True)
            rows_snapshot = copy.deepcopy(ws.state["patents"])
            settings_snapshot = copy.deepcopy(ws.settings)
        else:
            options, rows_snapshot, settings_snapshot = _prepared
        if not options["target_ids"]:
            return dict(
                skipped=True, status="completed", completed_count=0, total_count=0, judged_count=0, deferred_count=0
            )
        checkpoint = JudgmentCheckpoint(
            ws,
            options,
            settings_snapshot,
            save=save,
            report_progress=report_progress,
            checkpoint_batches=checkpoint_batches,
        )

    def request_judgment(system, payload):
        if settings_snapshot.get("provider") == "local":
            return complete(
                settings_snapshot,
                system,
                payload,
                response_schema=llm_judgment.response_schema(payload["required_ids"]),
            )
        return complete(settings_snapshot, system, payload)

    return llm_judgment.run(rows_snapshot, options, request_judgment, checkpoint.publish, ws.stop.is_set)


def run_model_training(
    ws, mode, include_agent=False, progress_callback=None, *, train=default_train, save=None, progress=None
):
    """Train using the same service from the map, agent and orchestration."""
    if mode not in ("lightweight", "transformer"):
        raise ValueError("学習方式は軽量学習かTransformerを選んでください。")
    save = ws.save if save is None else save
    report_progress = progress_callback or progress or (lambda value, message: default_progress(ws, value, message))
    state, stop = ws.state, ws.stop
    with ws.lock:
        rows = copy.deepcopy(state["patents"])
        settings = copy.deepcopy(ws.settings)
        job_id = ws.job["id"]
    result = train(
        rows,
        mode,
        settings["transformer_model"],
        settings["allow_model_download"],
        str(ws.data_dir / "models" / job_id),
        report_progress,
        stop.is_set,
        bool(include_agent),
    )
    with ws.lock:
        if stop.is_set():
            return dict(status="cancelled")
        with ws.transaction(("patents", "training"), check_idle=False, save=save):
            for row in state["patents"]:
                row["score"] = round(result["scores"][row["id"]], 4)
                row["score_source"] = mode
            result.pop("scores")
            result.update(mode=mode, job_id=job_id, status="completed")
            state["training"] = result
    return result
