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
    state, job, stop = ws.state, ws.job, ws.stop
    with ws.lock:
        if _prepared is None:
            options = assessment_options(ws, body, allow_empty=True)
            rows_snapshot = copy.deepcopy(state["patents"])
            settings_snapshot = copy.deepcopy(ws.settings)
        else:
            options, rows_snapshot, settings_snapshot = _prepared
        if not options["target_ids"]:
            return dict(
                skipped=True, status="completed", completed_count=0, total_count=0, judged_count=0, deferred_count=0
            )
        checkpoint_training = copy.deepcopy(state["training"])
        checkpoint_orchestration = copy.deepcopy(state.get("orchestration"))
        checkpoint_progress = {key: job[key] for key in ("progress", "message")}
    initialized = False
    pending_rows = {}
    pending_batches = 0
    saved_metadata = None
    checkpoint_failed = False

    def remember(row):
        # Keep one undo snapshot per changed row since the last durable checkpoint.
        if id(row) not in pending_rows:
            pending_rows[id(row)] = (row, copy.deepcopy(row))

    def publish(updates, metadata):
        nonlocal initialized, pending_batches, saved_metadata, checkpoint_failed
        nonlocal checkpoint_training, checkpoint_orchestration, checkpoint_progress
        with ws.lock:
            previous_status = (state.get("training") or {}).get("status")
            first_publish = not initialized
            temp_path = ws.data_dir / "workspace.tmp"
            attempted_save = False
            previous_temp = None
            try:
                if not initialized:
                    # An error-only report after failed initialization must
                    # preserve the scores that existed before this job.
                    if metadata["status"] == "running":
                        prior = state.get("training") or {}
                        same_judgment = (
                            prior.get("mode") == "llm"
                            and prior.get("criteria") == options["criteria"]
                            and prior.get("provider") == settings_snapshot.get("provider")
                            and prior.get("model") == settings_snapshot.get("model")
                            and prior.get("context_fingerprint") == options.get("context_fingerprint")
                        )
                        for row in state["patents"]:
                            if (
                                same_judgment
                                and row.get("label") in ("keep", "exclude")
                                and row.get("label_source") == "agent"
                                and row.get("score_source") == "llm"
                            ):
                                continue
                            remember(row)
                            row["score"] = None
                            row["score_source"] = None
                    initialized = True
                current = {row["id"]: row for row in state["patents"]} if updates else {}
                if updates and stop.is_set():
                    previous = state.get("training") or {}
                    for key in (
                        "completed_count",
                        "judged_count",
                        "deferred_count",
                        "protected_count",
                        "label_counts",
                        "progress",
                    ):
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
                    remember(row)
                    row.update({key: value for key, value in update.items() if key != "id"})
                if checkpoint_failed:
                    # llm_judgment may have accepted several in-memory batches.
                    # A failed checkpoint rolls ALL of those counts back too.
                    for key in ("completed_count", "judged_count", "deferred_count", "protected_count", "progress"):
                        metadata[key] = (saved_metadata or {}).get(key, 0)
                    metadata["label_counts"] = copy.deepcopy(
                        (saved_metadata or {}).get("label_counts", {"keep": 0, "exclude": 0})
                    )
                    metadata["message"] = (
                        f"{metadata['stage']} {metadata['completed_count']}/{metadata['total_count']}。"
                        "保存に失敗したため、最後の保存以降の未保存判定は取り消しました。"
                    )
                pending_batches += bool(updates)
                terminal = metadata["status"] in ("completed", "cancelled", "error")
                should_save = (
                    first_publish
                    or terminal
                    or metadata["status"] != previous_status
                    or pending_batches >= checkpoint_batches
                )
                metadata["saved_count"] = (
                    metadata["completed_count"] if should_save else (saved_metadata or {}).get("completed_count", 0)
                )
                if not terminal:
                    metadata["message"] += (
                        f"（保存済み {metadata['saved_count']}件。保存失敗・アプリ終了時は、"
                        "最後の保存以降の判定が失われます。）"
                    )
                metadata.update(
                    job_id=job["id"], provider=settings_snapshot["provider"], model=settings_snapshot.get("model", "")
                )
                state["training"] = metadata
                ws.invalidate_public_state()
                report_progress(metadata["progress"], metadata["message"])
                if should_save:
                    previous_temp = temp_path.read_bytes() if temp_path.exists() else None
                    attempted_save = True
                    save()
                    checkpoint_training = copy.deepcopy(metadata)
                    checkpoint_orchestration = copy.deepcopy(state.get("orchestration"))
                    checkpoint_progress = {key: job[key] for key in ("progress", "message")}
                    saved_metadata = copy.deepcopy(metadata)
                    pending_rows.clear()
                    pending_batches = 0
                    checkpoint_failed = False
            except Exception:
                for row, snapshot in pending_rows.values():
                    if row.get("label_source") == "human" and snapshot.get("label_source") != "human":
                        continue
                    row.clear()
                    row.update(snapshot)
                pending_rows.clear()
                pending_batches = 0
                checkpoint_failed = True
                state["training"] = copy.deepcopy(checkpoint_training)
                if isinstance(checkpoint_orchestration, dict) and isinstance(state.get("orchestration"), dict):
                    state["orchestration"].clear()
                    state["orchestration"].update(copy.deepcopy(checkpoint_orchestration))
                elif checkpoint_orchestration is not None:
                    state["orchestration"] = copy.deepcopy(checkpoint_orchestration)
                job.update(checkpoint_progress)
                ws.invalidate_public_state()
                initialized = saved_metadata is not None
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
                raise ValueError(
                    "LLM判定の保存に失敗しました。未保存のバッチを取り消し、直前に保存できた判定を保持しています。保存先の空き容量と書き込み権限を確認してください。"
                ) from None
            return copy.deepcopy(metadata)

    def request_judgment(system, payload):
        if settings_snapshot.get("provider") == "local":
            return complete(
                settings_snapshot,
                system,
                payload,
                response_schema=llm_judgment.response_schema(payload["required_ids"]),
            )
        return complete(settings_snapshot, system, payload)

    return llm_judgment.run(rows_snapshot, options, request_judgment, publish, stop.is_set)


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
        old_rows = copy.deepcopy(state["patents"])
        old_training = copy.deepcopy(state["training"])
        try:
            for row in state["patents"]:
                row["score"] = round(result["scores"][row["id"]], 4)
                row["score_source"] = mode
            result.pop("scores")
            result.update(mode=mode, job_id=job_id, status="completed")
            state["training"] = result
            save()
        except Exception:
            for row, previous in zip(state["patents"], old_rows):
                row.clear()
                row.update(previous)
            state["training"] = old_training
            raise
    return result
