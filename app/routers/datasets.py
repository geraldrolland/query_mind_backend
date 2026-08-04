"""Dataset routes: upload/clean, list, get, delete, records, profile, query."""

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Path, Query, Request, UploadFile, status
from sqlmodel import Session, delete, func, select

from app.cleaning.cleaner import clean_csv
from app.core.settings import settings
from app.db import get_session
from app.dsl.postgresjsonBcompiler import jsonBcompiler
from app.models.dataset import Dataset
from app.models.datasetrow import DatasetRow
from app.models.message import Message
from app.profiler.profiler import Profiler
from app.schemas.dataset import (
    CleaningReportSchema,
    DatasetOut,
    DatasetRowOut,
    RecordsResponseSchema,
    UploadResponseSchema,
)
from app.schemas.query import DslDefinitionSchema

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/datasets", tags=["datasets"])


def _dataset_out(d: Dataset) -> dict:
    return {
        "id": d.id,
        "name": d.name,
        "description": d.description,
        "populated": d.populated,
        "total_rows": d.total_rows,
        "total_size_bytes": d.total_size_bytes,
        "created_at": d.created_at.isoformat(),
        "updated_at": d.updated_at.isoformat(),
    }


def _require_owned(dataset: Dataset | None, user_id: int) -> Dataset:
    if not dataset or dataset.user_id != user_id:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return dataset


# ---------------------------------------------------------------------------
# POST /api/v1/datasets/upload
# ---------------------------------------------------------------------------

@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_dataset(
    request: Request,
    session: Session = Depends(get_session),
    file: UploadFile = File(..., description="CSV file to upload"),
    name: str = Form(..., min_length=1, max_length=255),
    description: str | None = Form(default=None, max_length=500),
):
    """Upload a CSV, clean it (dedupe + null counts), and store as a dataset."""
    user_id = request.state.auth_user["id"]

    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv files are supported")

    file_bytes = await file.read()
    if len(file_bytes) > settings.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 50MB limit")

    try:
        result = clean_csv(file_bytes, file.filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Unique dataset name per user.
    existing = session.exec(
        select(Dataset).where(Dataset.user_id == user_id, Dataset.name == name)
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Dataset with name '{name}' already exists")

    dataset = Dataset(
        user_id=user_id,
        name=name,
        description=description,
        dataset_schema=result.schema,
        populated=True,
        total_rows=result.rows_ingested,
        total_size_bytes=len(json.dumps(result.rows).encode()),
    )
    session.add(dataset)
    session.flush()

    # Bulk insert rows in batches.
    batch_size = 2000
    for i in range(0, len(result.rows), batch_size):
        batch = result.rows[i : i + batch_size]
        session.add_all(
            [
                DatasetRow(
                    dataset_id=dataset.id,
                    data=row,
                )
                for row in batch
            ]
        )
        session.flush()

    session.commit()
    session.refresh(dataset)

    from middleware import invalidate_user_cache

    invalidate_user_cache(user_id)

    cleaning_report = CleaningReportSchema(
        raw_rows=result.raw_rows,
        rows_ingested=result.rows_ingested,
        duplicates_removed=result.duplicates_removed,
        null_counts=result.null_counts,
        columns=result.columns,
    )

    return UploadResponseSchema(
        dataset=DatasetOut(**_dataset_out(dataset)),
        cleaning_report=cleaning_report,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/datasets
# ---------------------------------------------------------------------------

@router.get("")
async def list_datasets(request: Request, session: Session = Depends(get_session)):
    user_id = request.state.auth_user["id"]
    datasets = session.exec(
        select(Dataset).where(Dataset.user_id == user_id).order_by(Dataset.created_at.desc())
    ).all()
    return {"datasets": [_dataset_out(d) for d in datasets]}


# ---------------------------------------------------------------------------
# GET /api/v1/datasets/{dataset_id}
# ---------------------------------------------------------------------------

@router.get("/{dataset_id}")
async def get_dataset(
    request: Request,
    dataset_id: str = Path(...),
    session: Session = Depends(get_session),
):
    dataset = _require_owned(
        session.get(Dataset, dataset_id), request.state.auth_user["id"]
    )
    return _dataset_out(dataset)


# ---------------------------------------------------------------------------
# DELETE /api/v1/datasets/{dataset_id}
# ---------------------------------------------------------------------------

@router.delete("/{dataset_id}")
async def delete_dataset(
    request: Request,
    dataset_id: str = Path(...),
    session: Session = Depends(get_session),
):
    dataset = _require_owned(
        session.get(Dataset, dataset_id), request.state.auth_user["id"]
    )
    session.exec(
        delete(DatasetRow).where(DatasetRow.dataset_id == dataset_id)
    )
    session.delete(dataset)
    session.commit()

    from middleware import invalidate_user_cache

    invalidate_user_cache(request.state.auth_user["id"])
    return {"message": "Dataset deleted successfully"}


# ---------------------------------------------------------------------------
# GET /api/v1/datasets/{dataset_id}/records
# ---------------------------------------------------------------------------

@router.get("/{dataset_id}/records")
async def get_dataset_records(
    request: Request,
    dataset_id: str = Path(...),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    session: Session = Depends(get_session),
):
    dataset = _require_owned(
        session.get(Dataset, dataset_id), request.state.auth_user["id"]
    )

    total = session.exec(
        select(func.count()).select_from(DatasetRow).where(
            DatasetRow.dataset_id == dataset_id,
        )
    ).one()

    rows = session.exec(
        select(DatasetRow)
        .where(
            DatasetRow.dataset_id == dataset_id,
        )
        .order_by(DatasetRow.created_at)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    return RecordsResponseSchema(
        records=[
            DatasetRowOut(
                id=r.id, data=r.data, created_at=r.created_at.isoformat()
            )
            for r in rows
        ],
        total=int(total),
        page=page,
        page_size=page_size,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/datasets/{dataset_id}/messages
# ---------------------------------------------------------------------------

@router.get("/{dataset_id}/messages")
async def get_dataset_messages(
    request: Request,
    dataset_id: str = Path(...),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    session: Session = Depends(get_session),
):
    """Paginated chat messages for a dataset (50 per page, newest first)."""
    dataset = _require_owned(
        session.get(Dataset, dataset_id), request.state.auth_user["id"]
    )

    total = session.exec(
        select(func.count()).select_from(Message).where(
            Message.dataset_id == dataset_id
        )
    ).one()

    rows = session.exec(
        select(Message)
        .where(Message.dataset_id == dataset_id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    return {
        "messages": [
            {
                "id": m.id,
                "dataset_id": m.dataset_id,
                "content": m.content,
                "role": m.role,
                "type": m.type,
                "chart_type": m.chart_type,
                "is_error": m.is_error,
                "created_at": m.created_at.isoformat(),
            }
            for m in rows
        ],
        "total": int(total),
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# GET /api/v1/datasets/{dataset_id}/schema
# ---------------------------------------------------------------------------

@router.get("/{dataset_id}/schema")
async def get_dataset_schema(
    request: Request,
    dataset_id: str = Path(...),
    session: Session = Depends(get_session),
):
    dataset = _require_owned(
        session.get(Dataset, dataset_id), request.state.auth_user["id"]
    )
    return {"dataset_id": dataset.id, "schema": dataset.dataset_schema}


# ---------------------------------------------------------------------------
# GET /api/v1/datasets/{dataset_id}/profile
# ---------------------------------------------------------------------------

@router.get("/{dataset_id}/profile")
async def get_dataset_profile(
    request: Request,
    dataset_id: str = Path(...),
    session: Session = Depends(get_session),
):
    dataset = _require_owned(
        session.get(Dataset, dataset_id), request.state.auth_user["id"]
    )
    return Profiler(dataset_id=dataset.id).profile()


# ---------------------------------------------------------------------------
# POST /api/v1/datasets/{dataset_id}/query
# ---------------------------------------------------------------------------

@router.post("/{dataset_id}/query")
async def execute_query(
    request: Request,
    dsl_definition: DslDefinitionSchema,
    dataset_id: str = Path(...),
    session: Session = Depends(get_session),
):
    """Validate, compile, and execute a DSL query plan against the dataset."""
    dataset = _require_owned(
        session.get(Dataset, dataset_id), request.state.auth_user["id"]
    )
    if not dataset.populated:
        raise HTTPException(status_code=400, detail="Dataset is not populated yet")

    try:
        results = jsonBcompiler.execute_dsl(dsl_definition, dataset_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("Query execution failed for dataset %s: %s", dataset_id, exc)
        raise HTTPException(status_code=500, detail="Internal server error")

    data = results[0] if results else []
    return {"data": data}
