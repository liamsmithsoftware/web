from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class SiteCreateRequest(BaseModel):
    site_input: str = Field(..., min_length=1, description="آدرس وب‌سایت یا صفحه خبری / فید RSS")
    title: Optional[str] = Field(default=None, description="عنوان دلخواه وب‌سایت (اختیاری)")
    interval_value: float = Field(default=6.0, gt=0, description="مقدار بازه زمانی بررسی خودکار")
    interval_unit: Literal["minutes", "hours", "days"] = Field(default="hours")
    initial_limit: int = Field(default=10, ge=1, le=100, description="تعداد مطالب در اولین دریافت")
    is_active: bool = Field(default=True)
    fetch_now: bool = Field(default=True, description="آیا بلافاصله پس از افزودن، دریافت اولیه انجام شود؟")
    proxy_url: Optional[str] = Field(default=None, description="پروکسی اختصاصی سطح اپلیکیشن (اختیاری)")


class SiteUpdateRequest(BaseModel):
    title: Optional[str] = None
    interval_value: Optional[float] = Field(default=None, gt=0)
    interval_unit: Optional[Literal["minutes", "hours", "days"]] = None
    initial_limit: Optional[int] = Field(default=None, ge=1, le=100)
    is_active: Optional[bool] = None
    proxy_url: Optional[str] = Field(default=None, description="پروکسی اختصاصی سطح اپلیکیشن (برای تغییر پروکسی سایت)")


class SiteResetRequest(BaseModel):
    delete_posts: bool = Field(default=False)


class SiteProbeRequest(BaseModel):
    site_input: str = Field(..., min_length=1, description="آدرس وب‌سایت برای بررسی و تست اتصال")


class SettingsUpdateRequest(BaseModel):
    collector_mode: Optional[Literal["auto", "rss", "html"]] = None
    timezone: Optional[str] = None
    default_initial_limit: Optional[int] = Field(default=None, ge=1, le=100)
    fetch_full_content: Optional[bool] = None
    verify_ssl: Optional[bool] = None
    request_timeout: Optional[int] = Field(default=None, ge=5, le=120)
    custom_user_agent: Optional[str] = None
    custom_cookie: Optional[str] = None
    proxy_enabled: Optional[bool] = None
    proxy_type: Optional[Literal["socks5", "http"]] = None
    proxy_host: Optional[str] = None
    proxy_port: Optional[str] = None
    proxy_username: Optional[str] = None
    proxy_password: Optional[str] = None


class BulkDeleteRequest(BaseModel):
    """حذف گروهی مطالب: با فیلتر (وب‌سایت/تاریخ/نوع محتوا/جستجو) یا با انتخاب شناسه‌ها."""

    site_id: Optional[int] = Field(default=None, description="شناسه وب‌سایت (خالی = همه وب‌سایت‌ها)")
    date_from: Optional[str] = Field(default=None, description="از تاریخ (شمسی یا میلادی)")
    date_to: Optional[str] = Field(default=None, description="تا تاریخ (شمسی یا میلادی)")
    media_type: Optional[str] = Field(default=None, description="نوع محتوا (all یا خالی = همه)")
    search: Optional[str] = Field(default=None, description="جستجو در عنوان، متن یا آدرس مطلب")
    post_ids: Optional[List[int]] = Field(
        default=None,
        description="حذف فقط این شناسه‌های مطلب (انتخاب دستی از جدول آرشیو)",
    )
    reset_site_cursors: bool = Field(
        default=True,
        description="نشانگر آخرین مطلب وب‌سایت‌های متأثر ریست شود تا دریافت بعدی از ابتدا انجام شود",
    )


class PurgeRequest(BaseModel):
    """پاک‌سازی کلی آرشیو."""

    keep_sites: bool = Field(
        default=True,
        description="وب‌سایت‌ها حفظ شوند (فقط مطالب پاک شود) یا همه چیز حذف شود",
    )


class BackupRestoreRequest(BaseModel):
    """بازیابی فایل پشتیبان (محتوای JSON که از همان فایل خوانده شده)."""

    mode: Literal["merge", "replace"] = Field(
        default="merge",
        description="merge: فقط افزودن/به‌روزرسانی — replace: پاک کردن فهرست و بازگردانی کامل",
    )
    restore_settings: bool = Field(default=True, description="تنظیمات موجود در فایل هم اعمال شود")
    overwrite_settings: bool = Field(
        default=False,
        description="اگر true باشد، تنظیمات فعلی با مقادیر فایل جایگزین می‌شوند",
    )
    payload: Dict[str, Any] = Field(..., description="محتوای کامل فایل پشتیبان")
