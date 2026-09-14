"""Carry the caller's request context into a queued worker, never into job data."""
from contextvars import copy_context


def submit_with_context(executor, function, *args, **kwargs):
    context = copy_context()
    return executor.submit(context.run, function, *args, **kwargs)
