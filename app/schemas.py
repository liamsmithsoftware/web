from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field


class SiteCreateRequest(BaseModel):
    site_input: str = Field(..., min_length=1, description="آدرس وب‌سایت یا صفحه خبری / فید RSS")
    title: Optional[str] = Field(default=None, description="عنوان دلخواه وب‌سایت (اختیاری)")
    interval_value: float = Field(default=6.0, gt=0, description="مقدار بازه زمانی بررسی خودکار")
    interval_unit: Literal["minutes", "hours", "days"] = Field(default="hours")
    initial_limit: int = Field(default=10, ge=1, le=100, description="تعداد مطالب در اولین دریافت")
    is_active: bool = Field(default=True)
    fetch_now: bool = Field(default=True, description="آیا بلافاصله پس از افزودن، دریافت اولیه انجام شود؟")
    proxy_url: Optional[str] = Field(default=None, description="پروکسی دستی این وب‌سایت (اختیاری)")
    connection_profile_id: Optional[int] = Field(
        default=None, description="شناسه روش اتصال اختصاصی (خالی = حالت عمومی برنامه)"
    )


class SiteUpdateRequest(BaseModel):
    title: Optional[str] = None
    interval_value: Optional[float] = Field(default=None, gt=0)
    interval_unit: Optional[Literal["minutes", "hours", "days"]] = None
    initial_limit: Optional[int] = Field(default=None, ge=1, le=100)
    is_active: Optional[bool] = None
    proxy_url: Optional[str] = Field(default=None, description="پروکسی دستی این وب‌سایت (رشته خالی = پاک کردن)")
    connection_profile_id: Optional[int] = Field(
        default=None,
        description="شناسه روش اتصال اختصاصی؛ اگر صراحتاً null فرستاده شود، وب‌سایت به حالت عمومی برمی‌گردد",
    )


class SiteResetRequest(BaseModel):
    delete_posts: bool = Field(default=False)


class SiteProbeRequest(BaseModel):
    site_input: str = Field(..., min_length=1, description="آدرس وب‌سایت برای بررسی و تست اتصال")
    connection_profile_id: Optional[int] = Field(
        default=None, description="بررسی سایت از طریق یک روش اتصال اختصاصی (اختیاری)"
    )


class ConnectionProfileCreateRequest(BaseModel):
    """ثبت یک «روش اتصال» نام‌دار (پروکسی یا اتصال مستقیم) برای نسبت‌دادن به وب‌سایت‌ها."""

    name: str = Field(..., min_length=1, max_length=80, description="نام فارسی یا انگلیسی روش اتصال")
    kind: Literal["proxy", "direct"] = Field(default="proxy", description="proxy = پروکسی، direct = اتصال مستقیم")
    proxy_type: Literal["socks5", "http", "https"] = Field(default="socks5")
    proxy_host: str = Field(default="", description="آدرس میزبان یا IP پروکسی")
    proxy_port: Union[str, int, None] = Field(default="", description="پورت پروکسی")
    proxy_username: Optional[str] = Field(default="", description="نام کاربری پروکسی (اختیاری)")
    proxy_password: Optional[str] = Field(default=None, description="رمز عبور پروکسی (اختیاری)")
    verify_ssl: Optional[bool] = Field(default=None, description="None = همان تنظیمات عمومی برنامه")
    request_timeout: Optional[int] = Field(default=None, ge=5, le=120, description="None = همان تنظیمات عمومی")
    fallback_direct: bool = Field(default=True, description="در صورت شکست پروکسی، اتصال مستقیم هم امتحان شود")
    notes: str = Field(default="", max_length=500)
    is_enabled: bool = Field(default=True)


class ConnectionProfileUpdateRequest(BaseModel):
    """ویرایش روش اتصال؛ فیلدهای ارسال‌نشده بدون تغییر می‌مانند."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    kind: Optional[Literal["proxy", "direct"]] = None
    proxy_type: Optional[Literal["socks5", "http", "https"]] = None
    proxy_host: Optional[str] = None
    proxy_port: Optional[Union[str, int]] = None
    proxy_username: Optional[str] = None
    proxy_password: Optional[str] = Field(default=None, description="«********» یعنی رمز تغییر نکند")
    verify_ssl: Optional[bool] = Field(default=None, description="null = همان تنظیمات عمومی برنامه")
    request_timeout: Optional[int] = Field(default=None, ge=5, le=120)
    fallback_direct: Optional[bool] = None
    notes: Optional[str] = Field(default=None, max_length=500)
    is_enabled: Optional[bool] = None


class ConnectionProfileAssignRequest(BaseModel):
    """اعمال یک روش اتصال روی یک یا چند وب‌سایت انتخاب‌شده."""

    profile_id: Optional[int] = Field(
        default=None, description="شناسه روش اتصال؛ null = بازگرداندن وب‌سایت‌ها به حالت عمومی برنامه"
    )
    site_ids: List[int] = Field(default_factory=list, description="شناسه وب‌سایت‌های انتخاب‌شده")
    clear_manual_proxy: bool = Field(
        default=True, description="پروکسی دستی قدیمی وب‌سایت‌های انتخاب‌شده پاک شود تا تداخل نکند"
    )


class ConnectionProfileTestRequest(BaseModel):
    """تست اتصال یک روش اتصال (پروکسی) پیش از اعمال روی وب‌سایت‌ها."""

    target_url: Optional[str] = Field(
        default=None,
        description="آدرس دلخواه برای تست (خالی = سرویس سبک تشخیص IP خروجی)",
    )


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
