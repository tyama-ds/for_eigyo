"""Shared capacity limits for CSV batches, accumulated corpora, and display."""

MAX_IMPORT_ROWS = 20_000
MAX_DATASET_PAPERS = 200_000
# None means CSV uploads have no application-level byte ceiling.
MAX_UPLOAD_BYTES = None
MAX_ANALYSIS_YEARS = 50
LARGE_CORPUS_THRESHOLD = 10_000
PAPER_PAGE_LIMIT = 200


def public_limits() -> dict:
    return {"max_import_rows": MAX_IMPORT_ROWS,
            "max_dataset_papers": MAX_DATASET_PAPERS,
            "max_upload_bytes": MAX_UPLOAD_BYTES,
            "max_analysis_years": MAX_ANALYSIS_YEARS,
            "paper_page_limit": PAPER_PAGE_LIMIT}
