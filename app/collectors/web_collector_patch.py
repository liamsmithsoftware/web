    async def fetch_site_posts(
        self,
        *,\n        site: Dict[str, Any],\n        settings: Dict[str, str],\n    ) -> FetchBatchResult:
        mode = (settings.get("collector_mode") or "auto").strip().lower()
        tz_name = settings.get("timezone") or DEFAULT_TIMEZONE
        canonical_url = str(site.get("canonical_url") or "").strip()
        known_feed_url = str(site.get("feed_url") or "").strip() or None

        if not canonical_url:
            raise CollectorError("آدرس وب‌سایت نامعتبر یا خالی است.")

        # <--- جدید: اضافه کردن پروکسی اختصاصی سایت به تنظیمات موقت
        fetcher_settings = dict(settings)
        profile = get_site_profile(canonical_url)
        if profile and profile.proxy_url:
            fetcher_settings["site_proxy_url"] = profile.proxy_url
            fetcher_settings["explicit_proxy"] = ""
            fetcher_settings["use_system_proxy"] = "0"

        fetcher = SmartHttpFetcher(fetcher_settings)

        if mode == "html":
            return await self._fetch_via_html(fetcher, canonical_url, tz_name)

        if mode == "rss":
            return await self._fetch_via_rss_strict(
                fetcher, canonical_url, known_feed_url, tz_name
            )

        return await self._fetch_via_auto(
            fetcher, canonical_url, known_feed_url, tz_name
        )