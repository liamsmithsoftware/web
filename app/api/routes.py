from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response

from app.collectors.base import CollectorError
from app.collectors.manager import SyncManager
from app.config import DEFAULT_REQUEST_TIMEOUT, DEFAULT_TIMEZONE
from app.database import DatabaseManager
from app.schemas import (
    BackupRestoreRequest,
    BulkDeleteRequest,
    PurgeRequest,
    SettingsUpdateRequest,
    SiteCreateRequest,
    SiteProbeRequest,
    SiteResetRequest,
    SiteUpdateRequest,
)
from app.services.exporter import create_sqlite_export_snapshot, generate_excel_bytes
from app.services.scheduler import SiteSchedulerService
from app.utils.jalali import format_local_display, parse_filter_date_to_gregorian, utc_now
from app.utils.site_parser import normalize_digits, parse_site_input

router = APIRouter(prefix="/api")

_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def to_persian_number(value: Any) -> str:
    return str(value).translate(_FA_DIGITS)


def _get_db(request: Request) -> DatabaseManager:
    return request.app.state.db


def _get_sync_manager(request: Request) -> SyncManager:
    return request.app.state.sync_manager


def _get_scheduler(request: Request) -> SiteSchedulerService:
    return request.app.state.scheduler


def _enrich_site(site: Dict[str, Any], tz_name: str) -> Dict[str, Any]:
    out = dict(site)
    out["is_active"] = bool(site.get("is_active"))
    out["is_demo"] = bool(site.get("is_demo"))
    out["is_direct_feed"] = bool(site.get("is_direct_feed"))
    out["initial_sync_done"] = bool(site.get("initial_sync_done"))
    out["last_fetched_display"] = format_local_display(site.get("last_fetched_at"), tz_name=tz_name)
    out["next_fetch_display"] = format_local_display(site.get("next_fetch_at"), tz_name=tz_name)
    out["last_post_display"] = format_local_display(site.get("last_post_at"), tz_name=tz_name)
    return out


# ------------------------------------------------------------------
# Overview & Stats
# ------------------------------------------------------------------
@router.get("/overview")
async def get_overview(request: Request) -> Dict[str, Any]:
    db = _get_db(request)
    scheduler = _get_scheduler(request)

    settings = db.get_settings()
    tz_name = settings.get("timezone") or DEFAULT_TIMEZONE
    stats = db.get_dashboard_stats()
    stats["nearest_next_fetch_display"] = format_local_display(
        stats.get("nearest_next_fetch"), tz_name=tz_name
    )
    stats["latest_post_display"] = format_local_display(
        stats.get("latest_post_utc"), tz_name=tz_name
    )

    return {
        "stats": stats,
        "scheduler": scheduler.get_status(),
        "collector_mode": settings.get("collector_mode", "auto"),
        "fetch_full_content": settings.get("fetch_full_content", "1") == "1",
        "timezone": tz_name,
        "proxy_enabled": settings.get("proxy_enabled") == "1",
    }


# ------------------------------------------------------------------
# Websites Management
# ------------------------------------------------------------------
def _site_matches_filters(
    site: Dict[str, Any],
    *,
    search: Optional[str] = None,
    status: Optional[str] = None,
    source: Optional[str] = None,
) -> bool:
    if status and status != "all":
        st = status.strip().lower()
        is_active = bool(site.get("is_active"))
        last_status = str(site.get("last_status") or "").lower()
        if st == "inactive":
            if is_active:
                return False
        elif st == "ok":
            if not is_active or last_status not in ("ok", "success"):
                return False
        elif st == "error":
            if not is_active or last_status != "error":
                return False
        elif st == "pending":
            if not is_active or last_status != "pending":
                return False
        elif st == "syncing":
            if not is_active or last_status != "syncing":
                return False

    if source and source != "all":
        src = source.strip().lower()
        has_feed = bool(str(site.get("feed_url") or "").strip())
        if src == "has_feed" and not has_feed:
            return False
        if src == "no_feed" and has_feed:
            return False

    if search and search.strip():
        needle = normalize_digits(search).strip().lower()
        haystack = " ".join(
            normalize_digits(str(site.get(field) or "")).lower()
            for field in ("title", "canonical_url", "domain", "site_input", "feed_url", "last_error")
        )
        if needle not in haystack:
            return False

    return True


@router.get("/sites")
async def list_sites(
    request: Request,
    search: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    source: Optional[str] = Query(default=None),
    page: Optional[int] = Query(default=None, ge=1),
    page_size: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    db = _get_db(request)
    tz_name = db.get_settings().get("timezone") or DEFAULT_TIMEZONE
    all_items = [_enrich_site(s, tz_name) for s in db.list_sites()]

    filtered = [
        s
        for s in all_items
        if _site_matches_filters(s, search=search, status=status, source=source)
    ]
    total = len(filtered)

    # When neither `page` nor `page_size` is requested, return all matching items (backward-compatible)
    if page is None and page_size is None:
        return {
            "items": filtered,
            "total": total,
            "total_all": len(all_items),
            "page": 1,
            "page_size": "all",
            "total_pages": 1,
        }

    raw_ps = (page_size or "10").strip().lower()
    if raw_ps == "all":
        return {
            "items": filtered,
            "total": total,
            "total_all": len(all_items),
            "page": 1,
            "page_size": "all",
            "total_pages": 1,
        }

    try:
        ps_num = max(1, min(500, int(normalize_digits(raw_ps))))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="مقدار page_size معتبر نیست.") from exc

    total_pages = max(1, (total + ps_num - 1) // ps_num) if total else 1
    eff_page = min(page or 1, total_pages)
    start = (eff_page - 1) * ps_num
    paged_items = filtered[start : start + ps_num]

    return {
        "items": paged_items,
        "total": total,
        "total_all": len(all_items),
        "page": eff_page,
        "page_size": ps_num,
        "total_pages": total_pages,
    }


@router.post("/sites")
async def create_site(payload: SiteCreateRequest, request: Request) -> Dict[str, Any]:
    db = _get_db(request)
    sync_manager = _get_sync_manager(request)
    tz_name = db.get_settings().get("timezone") or DEFAULT_TIMEZONE

    try:
        parsed = parse_site_input(payload.site_input)
        created = db.create_site(
            parsed=parsed,
            interval_value=payload.interval_value,
            interval_unit=payload.interval_unit,
            initial_limit=payload.initial_limit,
            title=payload.title,
            is_active=payload.is_active,
            proxy_url=payload.proxy_url,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    sync_result = None
    if payload.fetch_now:
        sync_result = await sync_manager.sync_site(int(created["id"]), trigger_type="manual")
        refreshed = db.get_site(int(created["id"]))
        if refreshed:
            created = refreshed

    return {
        "site": _enrich_site(created, tz_name),
        "sync_result": sync_result,
    }


@router.post("/sites/probe")
async def probe_site_endpoint(payload: SiteProbeRequest, request: Request) -> Dict[str, Any]:
    db = _get_db(request)
    sync_manager = _get_sync_manager(request)
    settings = db.get_settings()
    try:
        return await sync_manager.web_collector.probe_site(payload.site_input, settings=settings)
    except CollectorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/sites/{site_id}")
async def update_site(
    site_id: int,
    payload: SiteUpdateRequest,
    request: Request,
) -> Dict[str, Any]:
    db = _get_db(request)
    tz_name = db.get_settings().get("timezone") or DEFAULT_TIMEZONE
    try:
        updated = db.update_site(
            site_id,
            title=payload.title,
            interval_value=payload.interval_value,
            interval_unit=payload.interval_unit,
            initial_limit=payload.initial_limit,
            is_active=payload.is_active,
            proxy_url=payload.proxy_url,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"site": _enrich_site(updated, tz_name)}


@router.delete("/sites/{site_id}")
async def delete_site(site_id: int, request: Request) -> Dict[str, Any]:
    db = _get_db(request)
    ok = db.delete_site(site_id)
    if not ok:
        raise HTTPException(status_code=404, detail="وب‌سایت مورد نظر یافت نشد.")
    return {"deleted": True, "site_id": site_id}


@router.post("/sites/sync-all")
async def sync_all_sites(request: Request) -> Dict[str, Any]:
    sync_manager = _get_sync_manager(request)
    return await sync_manager.sync_all_active(trigger_type="manual")


@router.post("/sites/{site_id}/sync")
async def sync_single_site(site_id: int, request: Request) -> Dict[str, Any]:
    db = _get_db(request)
    sync_manager = _get_sync_manager(request)
    tz_name = db.get_settings().get("timezone") or DEFAULT_TIMEZONE

    try:
        result = await sync_manager.sync_site(site_id, trigger_type="manual")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    updated_site = db.get_site(site_id)
    return {
        "sync_result": result,
        "site": _enrich_site(updated_site, tz_name) if updated_site else None,
    }


@router.post("/sites/{site_id}/reset")
async def reset_site_cursor(
    site_id: int,
    payload: SiteResetRequest,
    request: Request,
) -> Dict[str, Any]:
    db = _get_db(request)
    tz_name = db.get_settings().get("timezone") or DEFAULT_TIMEZONE
    try:
        updated = db.reset_site_cursor(site_id, delete_posts=payload.delete_posts)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"site": _enrich_site(updated, tz_name)}


# ------------------------------------------------------------------
# Posts Archive
# ------------------------------------------------------------------
@router.get("/posts")
async def list_posts(
    request: Request,
    site_id: Optional[int] = Query(default=None),
    search: Optional[str] = Query(default=None),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    media_type: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
) -> Dict[str, Any]:
    db = _get_db(request)
    tz_name = db.get_settings().get("timezone") or DEFAULT_TIMEZONE

    try:
        greg_from = parse_filter_date_to_gregorian(date_from)
        greg_to = parse_filter_date_to_gregorian(date_to)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    res = db.list_posts(
        site_id=site_id,
        search=search,
        date_from_greg=greg_from,
        date_to_greg=greg_to,
        media_type=media_type,
        page=page,
        page_size=page_size,
    )
    for item in res["items"]:
        item["fetched_display"] = format_local_display(item.get("fetched_at"), tz_name=tz_name)
    return res


@router.delete("/posts/{post_id}")
async def delete_post(post_id: int, request: Request) -> Dict[str, Any]:
    db = _get_db(request)
    if not db.delete_post(post_id):
        raise HTTPException(status_code=404, detail="مطلب مورد نظر یافت نشد.")
    return {"deleted": True, "post_id": post_id}


@router.post("/posts/bulk-delete")
async def bulk_delete_posts(payload: BulkDeleteRequest, request: Request) -> Dict[str, Any]:
    db = _get_db(request)

    try:
        greg_from = parse_filter_date_to_gregorian(payload.date_from)
        greg_to = parse_filter_date_to_gregorian(payload.date_to)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if greg_from and greg_to and greg_from > greg_to:
        raise HTTPException(status_code=400, detail="تاریخ شروع نمی‌تواند بعد از تاریخ پایان باشد.")

    if payload.site_id is not None and not db.get_site(int(payload.site_id)):
        raise HTTPException(status_code=404, detail="وب‌سایت انتخاب‌شده یافت نشد.")

    if payload.post_ids is not None and len(payload.post_ids) == 0:
        raise HTTPException(
            status_code=400,
            detail="هیچ مطلبی برای حذف انتخاب نشده است. ابتدا در جدول آرشیو، ردیف‌ها را تیک بزنید.",
        )

    media_type = payload.media_type if payload.media_type and payload.media_type != "all" else None

    matched = db.count_posts_for_scope(
        site_id=payload.site_id,
        date_from_greg=greg_from,
        date_to_greg=greg_to,
        media_type=media_type,
        search=payload.search,
        post_ids=payload.post_ids,
    )
    result = db.delete_posts_by_scope(
        site_id=payload.site_id,
        date_from_greg=greg_from,
        date_to_greg=greg_to,
        media_type=media_type,
        search=payload.search,
        post_ids=payload.post_ids,
        reset_site_cursors=payload.reset_site_cursors,
    )

    if not result["deleted_posts"]:
        message = "هیچ مطلبی مطابق با شرایط انتخاب‌شده یافت نشد."
    elif payload.post_ids is not None:
        message = f"{to_persian_number(result['deleted_posts'])} مطلب انتخاب‌شده حذف شد."
    else:
        message = f"{to_persian_number(result['deleted_posts'])} مطلب حذف شد."

    return {
        **result,
        "matched_before_delete": matched,
        "message": message,
    }


@router.get("/posts/ids")
async def list_post_ids(
    request: Request,
    site_id: Optional[int] = Query(default=None),
    search: Optional[str] = Query(default=None),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    media_type: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    db = _get_db(request)
    try:
        greg_from = parse_filter_date_to_gregorian(date_from)
        greg_to = parse_filter_date_to_gregorian(date_to)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    media = media_type if media_type and media_type != "all" else None
    posts = db.get_posts_for_export(
        site_id=site_id,
        search=search,
        date_from_greg=greg_from,
        date_to_greg=greg_to,
        media_type=media,
    )
    return {"ids": [int(p["id"]) for p in posts], "total": len(posts)}


@router.post("/posts/purge")
async def purge_archive(payload: PurgeRequest, request: Request) -> Dict[str, Any]:
    db = _get_db(request)
    result = db.purge_everything(keep_sites=payload.keep_sites)

    if payload.keep_sites:
        message = (
            f"تمام مطالب ({to_persian_number(result['deleted_posts'])} مورد) و گزارش‌ها پاک شد. "
            "وب‌سایت‌ها حفظ شدند و نشانگر آخرین مطلب آن‌ها ریست شد."
        )
    else:
        message = (
            f"همه چیز پاک شد: {to_persian_number(result['deleted_sites'])} وب‌سایت، "
            f"{to_persian_number(result['deleted_posts'])} مطلب و "
            f"{to_persian_number(result['deleted_logs'])} گزارش."
        )
    return {**result, "message": message}


# ------------------------------------------------------------------
# Backup / Restore (sites + settings)
# ------------------------------------------------------------------
MAX_BACKUP_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB


@router.get("/backup/export")
async def export_backup(
    request: Request,
    include_settings: bool = Query(default=True, description="تنظیمات هم در فایل باشد"),
    include_secrets: bool = Query(
        default=False,
        description="اطلاعات حساس (رمز پروکسی و کوکی نشست) هم ذخیره شوند — پیش‌فرض: خیر",
    ),
    only_active: bool = Query(default=False, description="فقط وب‌سایت‌های فعال"),
) -> Response:
    db = _get_db(request)
    payload = db.create_backup_payload(
        include_settings=include_settings,
        include_secrets=include_secrets,
        only_active=only_active,
    )

    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    stamp = utc_now().strftime("%Y%m%d_%H%M%S")
    filename = f"web_backup_{stamp}.json"
    return Response(
        content=body,
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/backup/inspect")
async def inspect_backup(request: Request) -> Dict[str, Any]:
    db = _get_db(request)
    payload = db.create_backup_payload(include_settings=True, include_secrets=False)
    return {
        "format": payload["format"],
        "format_version": payload["format_version"],
        "created_at": payload["created_at"],
        "counts": payload["counts"],
        "sites": [
            {
                "title": s["title"],
                "site_input": s["site_input"],
                "interval_value": s["interval_value"],
                "interval_unit": s["interval_unit"],
                "is_active": s["is_active"],
                "last_item_seq": s["last_item_seq"],
            }
            for s in payload["sites"]
        ],
        "settings": payload["settings"],
    }


def _parse_backup_bytes(raw: bytes) -> Dict[str, Any]:
    if not raw:
        raise HTTPException(status_code=400, detail="فایل پشتیبان خالی است.")
    if len(raw) > MAX_BACKUP_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="حجم فایل پشتیبان بیش از حد مجاز (۵ مگابایت) است.")
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=400,
            detail="فایل انتخاب‌شده یک JSON معتبر نیست. لطفاً همان فایل پشتیبان برنامه را انتخاب کنید.",
        ) from exc


def _run_restore(db: DatabaseManager, data: Any, options: Dict[str, Any]) -> Dict[str, Any]:
    try:
        report = db.restore_from_backup(data, **options)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    summary_bits = []
    if report["added_sites"]:
        summary_bits.append(f"{to_persian_number(report['added_sites'])} وب‌سایت جدید اضافه شد")
    if report["updated_sites"]:
        summary_bits.append(f"{to_persian_number(report['updated_sites'])} وب‌سایت به‌روزرسانی شد")
    if report["removed_sites"]:
        summary_bits.append(f"{to_persian_number(report['removed_sites'])} وب‌سایت قبلی حذف شد")
    if report["restored_settings"]:
        summary_bits.append(f"{to_persian_number(report['restored_settings'])} تنظیم اعمال شد")
    if report["skipped_sites"]:
        summary_bits.append(f"{to_persian_number(report['skipped_sites'])} مورد نامعتبر نادیده گرفته شد")

    report["message"] = (
        "بازیابی با موفقیت انجام شد: " + "، ".join(summary_bits) + "."
        if summary_bits
        else "بازیابی انجام شد ولی فایل، وب‌سایت یا تنظیم قابل اعمالی نداشت."
    )
    return report


@router.post("/backup/restore")
async def restore_backup(payload: BackupRestoreRequest, request: Request) -> Dict[str, Any]:
    db = _get_db(request)
    return _run_restore(
        db,
        payload.payload,
        {
            "mode": payload.mode,
            "restore_settings": payload.restore_settings,
            "overwrite_settings": payload.overwrite_settings,
        },
    )


@router.post("/backup/restore-file")
async def restore_backup_from_file(
    request: Request,
    file: UploadFile = File(..., description="فایل پشتیبان .json"),
    mode: str = Query(default="merge", pattern="^(merge|replace)$"),
    restore_settings: bool = Query(default=True),
    overwrite_settings: bool = Query(default=False),
) -> Dict[str, Any]:
    db = _get_db(request)
    raw = await file.read()
    data = _parse_backup_bytes(raw)
    return _run_restore(
        db,
        data,
        {
            "mode": mode,
            "restore_settings": restore_settings,
            "overwrite_settings": overwrite_settings,
        },
    )


@router.post("/backup/validate-file")
async def validate_backup_file(
    request: Request,
    file: UploadFile = File(..., description="فایل پشتیبان .json"),
) -> Dict[str, Any]:
    db = _get_db(request)
    raw = await file.read()
    data = _parse_backup_bytes(raw)
    try:
        parsed = db.validate_backup_payload(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    existing_keys = {s["site_key"] for s in db.list_sites()}
    new_count = 0
    for raw_site in parsed["sites"]:
        try:
            key = parse_site_input(raw_site["site_input"]).site_key
        except ValueError:
            continue
        if key not in existing_keys:
            new_count += 1

    return {
        "valid": True,
        "format_version": parsed["format_version"],
        "created_at": parsed["created_at"],
        "note": parsed["note"],
        "sites_in_file": len(parsed["sites"]),
        "new_sites": new_count,
        "existing_sites": len(parsed["sites"]) - new_count,
        "settings_in_file": len(parsed["settings"]),
        "secrets_included": any(
            key in parsed["settings"]
            for key in ("proxy_username", "proxy_password", "custom_cookie")
        ),
        "settings_keys": sorted(parsed["settings"].keys()),
        "message": (
            f"فایل معتبر است: {to_persian_number(len(parsed['sites']))} وب‌سایت "
            f"({to_persian_number(new_count)} جدید) و "
            f"{to_persian_number(len(parsed['settings']))} تنظیم."
        ),
    }


# ------------------------------------------------------------------
# Fetch Logs
# ------------------------------------------------------------------
@router.get("/logs")
async def get_logs(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    site_id: Optional[int] = Query(default=None),
) -> Dict[str, Any]:
    db = _get_db(request)
    tz_name = db.get_settings().get("timezone") or DEFAULT_TIMEZONE
    logs = db.list_fetch_logs(limit=limit, site_id=site_id)
    for lg in logs:
        lg["created_display"] = format_local_display(lg.get("created_at"), tz_name=tz_name)
    return {"items": logs}


@router.delete("/logs")
async def clear_logs(request: Request) -> Dict[str, Any]:
    db = _get_db(request)
    removed = db.clear_fetch_logs()
    return {"cleared": removed}


# ------------------------------------------------------------------
# Exports: Excel (.xlsx) & SQLite (.sqlite)
# ------------------------------------------------------------------
@router.get("/exports/excel")
async def export_excel(
    request: Request,
    site_id: Optional[int] = Query(default=None),
    search: Optional[str] = Query(default=None),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    media_type: Optional[str] = Query(default=None),
) -> Response:
    db = _get_db(request)
    tz_name = db.get_settings().get("timezone") or DEFAULT_TIMEZONE

    try:
        greg_from = parse_filter_date_to_gregorian(date_from)
        greg_to = parse_filter_date_to_gregorian(date_to)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    posts = db.get_posts_for_export(
        site_id=site_id,
        search=search,
        date_from_greg=greg_from,
        date_to_greg=greg_to,
        media_type=media_type,
    )
    sites = db.list_sites()

    excel_bytes = generate_excel_bytes(posts=posts, sites=sites, tz_name=tz_name)
    stamp = utc_now().strftime("%Y%m%d_%H%M%S")
    filename = f"web_posts_{stamp}.xlsx"

    return Response(
        content=excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


@router.get("/exports/sqlite")
async def export_sqlite(
    request: Request,
    background_tasks: BackgroundTasks,
    filtered_only: bool = Query(default=False),
    site_id: Optional[int] = Query(default=None),
    search: Optional[str] = Query(default=None),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    media_type: Optional[str] = Query(default=None),
) -> FileResponse:
    db = _get_db(request)

    filtered_ids = None
    if filtered_only:
        try:
            greg_from = parse_filter_date_to_gregorian(date_from)
            greg_to = parse_filter_date_to_gregorian(date_to)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        matching = db.get_posts_for_export(
            site_id=site_id,
            search=search,
            date_from_greg=greg_from,
            date_to_greg=greg_to,
            media_type=media_type,
        )
        filtered_ids = [int(p["id"]) for p in matching]

    snapshot_path = create_sqlite_export_snapshot(
        db,
        filtered_post_ids=filtered_ids,
        strip_sensitive_settings=True,
    )
    background_tasks.add_task(_unlink_quietly, snapshot_path)

    stamp = utc_now().strftime("%Y%m%d_%H%M%S")
    filename = f"web_collector_{stamp}.sqlite"

    return FileResponse(
        path=str(snapshot_path),
        media_type="application/vnd.sqlite3",
        filename=filename,
        headers={"Cache-Control": "no-store"},
    )


# ------------------------------------------------------------------
# Settings
# ------------------------------------------------------------------
@router.get("/settings")
async def get_settings_endpoint(request: Request) -> Dict[str, Any]:
    db = _get_db(request)
    raw = db.get_settings()

    return {
        "settings": {
            "collector_mode": raw.get("collector_mode", "auto"),
            "timezone": raw.get("timezone", DEFAULT_TIMEZONE),
            "default_initial_limit": int(raw.get("default_initial_limit") or 10),
            "fetch_full_content": raw.get("fetch_full_content", "1") == "1",
            "verify_ssl": raw.get("verify_ssl", "1") == "1",
            "request_timeout": int(raw.get("request_timeout") or DEFAULT_REQUEST_TIMEOUT),
            "custom_user_agent": raw.get("custom_user_agent", ""),
            "custom_cookie_set": bool(raw.get("custom_cookie")),
            "proxy_enabled": raw.get("proxy_enabled") == "1",
            "proxy_type": raw.get("proxy_type", "socks5"),
            "proxy_host": raw.get("proxy_host", ""),
            "proxy_port": raw.get("proxy_port", ""),
            "proxy_username": raw.get("proxy_username", ""),
            "proxy_password_set": bool(raw.get("proxy_password")),
        }
    }


@router.put("/settings")
async def update_settings_endpoint(
    payload: SettingsUpdateRequest,
    request: Request,
) -> Dict[str, Any]:
    db = _get_db(request)
    updates: Dict[str, Any] = {}

    if payload.collector_mode is not None:
        updates["collector_mode"] = payload.collector_mode
    if payload.timezone is not None and payload.timezone.strip():
        updates["timezone"] = payload.timezone.strip()
    if payload.default_initial_limit is not None:
        updates["default_initial_limit"] = str(payload.default_initial_limit)
    if payload.fetch_full_content is not None:
        updates["fetch_full_content"] = "1" if payload.fetch_full_content else "0"
    if payload.verify_ssl is not None:
        updates["verify_ssl"] = "1" if payload.verify_ssl else "0"
    if payload.request_timeout is not None:
        updates["request_timeout"] = str(payload.request_timeout)
    if payload.custom_user_agent is not None:
        updates["custom_user_agent"] = payload.custom_user_agent.strip()
    if payload.custom_cookie is not None and payload.custom_cookie != "********":
        updates["custom_cookie"] = payload.custom_cookie.strip()

    if payload.proxy_enabled is not None:
        updates["proxy_enabled"] = "1" if payload.proxy_enabled else "0"
    if payload.proxy_type is not None:
        updates["proxy_type"] = payload.proxy_type
    if payload.proxy_host is not None:
        updates["proxy_host"] = payload.proxy_host.strip()
    if payload.proxy_port is not None:
        clean_port = normalize_digits(payload.proxy_port).strip()
        if clean_port and (not clean_port.isdigit() or not (1 <= int(clean_port) <= 65535)):
            raise HTTPException(status_code=400, detail="پورت پروکسی باید عددی بین ۱ تا ۶۵۵۳۵ باشد.")
        updates["proxy_port"] = clean_port
    if payload.proxy_username is not None:
        updates["proxy_username"] = payload.proxy_username.strip()
    if payload.proxy_password is not None and payload.proxy_password != "********":
        updates["proxy_password"] = payload.proxy_password

    db.update_settings(updates)
    return await get_settings_endpoint(request)


# ------------------------------------------------------------------
# Demo Seed / Clear (for instant UI & export testing)
# ------------------------------------------------------------------
@router.post("/demo/seed")
async def seed_demo(request: Request) -> Dict[str, Any]:
    db = _get_db(request)
    tz_name = db.get_settings().get("timezone") or DEFAULT_TIMEZONE
    res = db.seed_demo_data(tz_name=tz_name)
    return {
        "seeded": True,
        **res,
        "message": "داده‌های نمونه آزمایشی با موفقیت بارگذاری شد. اکنون می‌توانید فیلترها و خروجی Excel و SQLite را تست کنید.",
    }


@router.delete("/demo")
async def clear_demo(request: Request) -> Dict[str, Any]:
    db = _get_db(request)
    removed = db.clear_demo_data()
    return {
        "cleared_sites": removed,
        "message": "تمامی وب‌سایت‌ها و مطالب آزمایشی پاک‌سازی شدند.",
    }
