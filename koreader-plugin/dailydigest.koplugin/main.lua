--[[--
Daily Digest plugin for KOReader.

Automatically syncs daily EPUB digests (e.g. Hacker News, TLDR) from a hosted
`manifest.json` feed whenever the device connects to Wi-Fi, saves them into a
regular folder (default: /mnt/onboard/DailyReads on Kobo), and purges digests
older than `retention_days` (default: 4) along with their .sdr sidecars.

@module koplugin.dailydigest
--]]

local DataStorage = require("datastorage")
local Device = require("device")
local Dispatcher = require("dispatcher")
local DocSettings = require("docsettings")
local FileManager = require("apps/filemanager/filemanager")
local InfoMessage = require("ui/widget/infomessage")
local InputDialog = require("ui/widget/inputdialog")
local JSON = require("json")
local LuaSettings = require("luasettings")
local NetworkMgr = require("ui/network/manager")
local ReadHistory = require("readhistory")
local SpinWidget = require("ui/widget/spinwidget")
local UIManager = require("ui/uimanager")
local WidgetContainer = require("ui/widget/container/widgetcontainer")
local ffiUtil = require("ffi/util")
local http = require("socket.http")
local lfs = require("libs/libkoreader-lfs")
local logger = require("logger")
local ltn12 = require("ltn12")
local socket = require("socket")
local socket_url = require("socket.url")
local socketutil = require("socketutil")
local util = require("util")
local _ = require("gettext")
local T = ffiUtil.template

local DEFAULT_FEED_URL = "https://conhuang.github.io/kobo-daily-digest/digests/manifest.json"
local DEFAULT_RETENTION_DAYS = 4
local AUTO_SYNC_COOLDOWN_SEC = 1800 -- 30 minutes
local DIGEST_FILENAME_PATTERN = "^(%d%d%d%d)%-(%d%d)%-(%d%d)%-[%w_%-]+%.epub$"

local DailyDigest = WidgetContainer:extend{
    name = "dailydigest",
    settings_file = DataStorage:getSettingsDir() .. "/dailydigest.lua",
    syncing = false,
}

local function getDefaultDownloadDir()
    if Device:isKobo() then
        return (Device.home_dir or "/mnt/onboard") .. "/DailyReads"
    end
    return DataStorage:getFullDataDir() .. "/DailyReads"
end

local function trimSlash(path)
    if not path then return "" end
    return (path:gsub("/+$", ""))
end

local function parseDigestDateSeconds(filename)
    local y, m, d = filename:match(DIGEST_FILENAME_PATTERN)
    if not y then return nil end
    return os.time({
        year = tonumber(y),
        month = tonumber(m),
        day = tonumber(d),
        hour = 0,
        min = 0,
        sec = 0,
    })
end

local function getTodayMidnightSeconds()
    local now = os.date("*t")
    return os.time({
        year = now.year,
        month = now.month,
        day = now.day,
        hour = 0,
        min = 0,
        sec = 0,
    })
end

function DailyDigest:init()
    self:loadSettings()
    self:onDispatcherRegisterActions()
    self.ui.menu:registerToMainMenu(self)
end

function DailyDigest:loadSettings()
    self.settings = LuaSettings:open(self.settings_file)
    self.feed_url = self.settings:readSetting("feed_url") or DEFAULT_FEED_URL
    self.download_dir = trimSlash(self.settings:readSetting("download_dir") or getDefaultDownloadDir())
    self.retention_days = tonumber(self.settings:readSetting("retention_days")) or DEFAULT_RETENTION_DAYS
    if self.settings:has("auto_sync") then
        self.auto_sync = self.settings:isTrue("auto_sync")
    else
        self.auto_sync = true
    end
    self.last_sync_ts = tonumber(self.settings:readSetting("last_sync_ts")) or 0
    self.last_sync_date = self.settings:readSetting("last_sync_date") or ""
end

function DailyDigest:saveSettings()
    self.settings:saveSetting("feed_url", self.feed_url)
    self.settings:saveSetting("download_dir", self.download_dir)
    self.settings:saveSetting("retention_days", self.retention_days)
    self.settings:saveSetting("auto_sync", self.auto_sync)
    self.settings:saveSetting("last_sync_ts", self.last_sync_ts)
    self.settings:saveSetting("last_sync_date", self.last_sync_date)
    self.settings:flush()
end

function DailyDigest:onDispatcherRegisterActions()
    Dispatcher:registerAction("daily_digest_sync", {
        category = "none",
        event = "DailyDigestSync",
        title = _("Daily Digest: Sync now"),
        general = true,
    })
end

function DailyDigest:onDailyDigestSync()
    NetworkMgr:runWhenOnline(function()
        self:syncDigests(false)
    end)
    return true
end

function DailyDigest:onNetworkConnected()
    if not self.auto_sync then
        return
    end
    if not self.feed_url or self.feed_url == "" then
        return
    end

    local now = os.time()
    local today = os.date("%Y-%m-%d")
    if self.last_sync_date == today and (now - self.last_sync_ts) < AUTO_SYNC_COOLDOWN_SEC then
        logger.dbg("DailyDigest: skipping auto-sync (within cooldown)")
        return
    end

    logger.info("DailyDigest: Wi-Fi connected, scheduling auto-sync")
    UIManager:scheduleIn(2, function()
        if NetworkMgr:isOnline() then
            self:syncDigests(true)
        end
    end)
end

function DailyDigest:addToMainMenu(menu_items)
    menu_items.daily_digest = {
        text = _("Daily Digest"),
        sorting_hint = "tools",
        sub_item_table_func = function()
            return self:getSubMenuItems()
        end,
    }
end

function DailyDigest:getSubMenuItems()
    return {
        {
            text = _("Sync now"),
            keep_menu_open = false,
            callback = function()
                if not self.feed_url or self.feed_url == "" then
                    self:editFeedUrl()
                    return
                end
                NetworkMgr:runWhenOnline(function()
                    self:syncDigests(false)
                end)
            end,
        },
        {
            text = _("Open DailyReads folder"),
            callback = function()
                self:ensureDownloadDir()
                if self.ui.document then
                    self.ui:onClose()
                end
                FileManager:showFiles(self.download_dir)
            end,
        },
        {
            text = _("Purge expired digests now"),
            keep_menu_open = true,
            callback = function()
                local purged = self:purgeExpiredLocal()
                self:refreshFileManagerIfNeeded()
                UIManager:show(InfoMessage:new{
                    text = T(_("Purged %1 expired digest(s)."), purged),
                    timeout = 3,
                })
            end,
        },
        {
            text = _("Settings"),
            sub_item_table = {
                {
                    text_func = function()
                        local display = (self.feed_url ~= "" and self.feed_url) or _("(not set)")
                        return T(_("Feed URL: %1"), display)
                    end,
                    keep_menu_open = true,
                    callback = function(touchmenu_instance)
                        self:editFeedUrl(touchmenu_instance)
                    end,
                },
                {
                    text_func = function()
                        return T(_("Folder: %1"), self.download_dir)
                    end,
                    keep_menu_open = true,
                    callback = function(touchmenu_instance)
                        self:chooseDownloadDir(touchmenu_instance)
                    end,
                },
                {
                    text_func = function()
                        return T(_("Keep digests for %1 days"), self.retention_days)
                    end,
                    keep_menu_open = true,
                    callback = function(touchmenu_instance)
                        self:editRetentionDays(touchmenu_instance)
                    end,
                },
                {
                    text = _("Auto-sync on Wi-Fi connect"),
                    checked_func = function()
                        return self.auto_sync
                    end,
                    callback = function()
                        self.auto_sync = not self.auto_sync
                        self:saveSettings()
                    end,
                },
            },
        },
    }
end

function DailyDigest:editFeedUrl(touchmenu_instance)
    local dialog
    dialog = InputDialog:new{
        title = _("Daily Digest manifest.json URL"),
        input = self.feed_url,
        input_hint = "https://<user>.github.io/kobo-daily-digest/digests/manifest.json",
        buttons = {
            {
                {
                    text = _("Cancel"),
                    id = "close",
                    callback = function()
                        UIManager:close(dialog)
                    end,
                },
                {
                    text = _("Save"),
                    is_enter_default = true,
                    callback = function()
                        local val = util.trim(dialog:getInputText() or "")
                        self.feed_url = val
                        self:saveSettings()
                        UIManager:close(dialog)
                        if touchmenu_instance then
                            touchmenu_instance:updateItems()
                        end
                    end,
                },
            },
        },
    }
    UIManager:show(dialog)
    dialog:onShowKeyboard()
end

function DailyDigest:chooseDownloadDir(touchmenu_instance)
    self:ensureDownloadDir()
    require("ui/downloadmgr"):new{
        onConfirm = function(path)
            self.download_dir = trimSlash(path)
            self:saveSettings()
            if touchmenu_instance then
                touchmenu_instance:updateItems()
            end
        end,
    }:chooseDir(self.download_dir)
end

function DailyDigest:editRetentionDays(touchmenu_instance)
    local spin = SpinWidget:new{
        value = self.retention_days,
        value_min = 1,
        value_max = 30,
        default_value = DEFAULT_RETENTION_DAYS,
        title_text = _("Retention period (days)"),
        info_text = _("Digests older than this many days will be deleted from your Kobo."),
        callback = function(widget)
            self.retention_days = widget.value
            self:saveSettings()
            if touchmenu_instance then
                touchmenu_instance:updateItems()
            end
        end,
    }
    UIManager:show(spin)
end

function DailyDigest:ensureDownloadDir()
    if lfs.attributes(self.download_dir, "mode") ~= "directory" then
        util.makePath(self.download_dir)
    end
end

function DailyDigest:httpGetToString(url)
    local sink = {}
    socketutil:set_timeout(socketutil.LARGE_BLOCK_TIMEOUT, socketutil.LARGE_TOTAL_TIMEOUT)
    local code, headers, status = socket.skip(1, http.request{
        url = url,
        method = "GET",
        redirect = true,
        headers = {
            ["User-Agent"] = "KOReader-DailyDigest/1.0",
            ["Cache-Control"] = "no-cache",
        },
        sink = ltn12.sink.table(sink),
    })
    socketutil:reset_timeout()
    if not headers or tonumber(code) ~= 200 then
        return nil, string.format("HTTP error %s (%s)", tostring(code), tostring(status))
    end
    return table.concat(sink), nil
end

function DailyDigest:httpDownloadToFile(url, dest_path)
    local tmp_path = dest_path .. ".tmp"
    local fh, open_err = io.open(tmp_path, "wb")
    if not fh then
        return false, "Cannot open temp file: " .. tostring(open_err)
    end

    socketutil:set_timeout(socketutil.FILE_BLOCK_TIMEOUT, socketutil.FILE_TOTAL_TIMEOUT)
    local code, headers, status = socket.skip(1, http.request{
        url = url,
        method = "GET",
        redirect = true,
        headers = {
            ["User-Agent"] = "KOReader-DailyDigest/1.0",
        },
        sink = ltn12.sink.file(fh),
    })
    socketutil:reset_timeout()

    if not headers or tonumber(code) ~= 200 then
        os.remove(tmp_path)
        return false, string.format("HTTP error %s (%s)", tostring(code), tostring(status))
    end

    local size = lfs.attributes(tmp_path, "size") or 0
    if size <= 0 then
        os.remove(tmp_path)
        return false, "Downloaded file was empty"
    end

    os.remove(dest_path)
    local ok, ren_err = os.rename(tmp_path, dest_path)
    if not ok then
        os.remove(tmp_path)
        return false, "Failed renaming download: " .. tostring(ren_err)
    end
    return true, nil
end

function DailyDigest:deleteLocalDigest(full_path)
    if self.ui.document and self.ui.document.file == full_path then
        logger.info("DailyDigest: skipping purge of currently open file:", full_path)
        return false
    end
    if lfs.attributes(full_path, "mode") ~= "file" then
        return false
    end

    -- Prefer KOReader's FileManager:deleteFile (deletes file, .sdr sidecar, and ReadHistory).
    if FileManager and FileManager.deleteFile then
        FileManager:deleteFile(full_path, true)
        return true
    end

    -- Fallback cleanup if FileManager helper is unavailable.
    os.remove(full_path)
    local sidecar = DocSettings:getSidecarDir(full_path)
    if sidecar and lfs.attributes(sidecar, "mode") == "directory" then
        ffiUtil.purgeDir(sidecar)
    end
    ReadHistory:removeItemByPath(full_path)
    return true
end

function DailyDigest:purgeExpiredLocal()
    if lfs.attributes(self.download_dir, "mode") ~= "directory" then
        return 0
    end

    local today_sec = getTodayMidnightSeconds()
    local max_age_sec = self.retention_days * 86400
    local purged = 0

    for entry in lfs.dir(self.download_dir) do
        local file_date_sec = parseDigestDateSeconds(entry)
        if file_date_sec then
            local age_sec = today_sec - file_date_sec
            if age_sec >= max_age_sec then
                local full_path = self.download_dir .. "/" .. entry
                logger.info("DailyDigest: purging expired digest:", full_path)
                if self:deleteLocalDigest(full_path) then
                    purged = purged + 1
                end
            end
        end
    end

    return purged
end

function DailyDigest:refreshFileManagerIfNeeded()
    if FileManager.instance and FileManager.instance.file_chooser then
        local current_dir = trimSlash(FileManager.instance.file_chooser.path)
        if current_dir == self.download_dir then
            FileManager.instance:onRefresh()
        end
    end
end

function DailyDigest:syncDigests(is_auto)
    if self.syncing then
        return
    end
    if not self.feed_url or self.feed_url == "" then
        if not is_auto then
            UIManager:show(InfoMessage:new{
                text = _("Please configure the Daily Digest Feed URL in Settings first."),
            })
        end
        return
    end

    self.syncing = true
    self:ensureDownloadDir()

    local progress_msg
    if not is_auto then
        progress_msg = InfoMessage:new{ text = _("Syncing daily digests…") }
        UIManager:show(progress_msg)
        UIManager:forceRePaint()
    end

    local ok, err = pcall(function()
        local body, fetch_err = self:httpGetToString(self.feed_url)
        if not body then
            error(fetch_err or "Failed to fetch manifest.json")
        end

        local manifest = JSON.decode(body)
        if type(manifest) ~= "table" or type(manifest.digests) ~= "table" then
            error("Invalid manifest.json format")
        end

        local today_sec = getTodayMidnightSeconds()
        local max_age_sec = self.retention_days * 86400
        local downloaded = 0
        local failed = 0

        for _, item in ipairs(manifest.digests) do
            local filename = type(item) == "table" and item.filename or nil
            if filename and filename:match(DIGEST_FILENAME_PATTERN) then
                local file_date_sec = parseDigestDateSeconds(filename)
                local is_expired = file_date_sec and ((today_sec - file_date_sec) >= max_age_sec)

                if not is_expired then
                    local dest_path = self.download_dir .. "/" .. filename
                    local local_size = lfs.attributes(dest_path, "size")
                    local remote_size = tonumber(item.size_bytes)
                    local needs_download = (not local_size) or (remote_size and local_size ~= remote_size)

                    if needs_download then
                        local item_url = socket_url.absolute(self.feed_url, item.url or filename)
                        logger.info("DailyDigest: downloading", item_url, "->", dest_path)
                        local dl_ok, dl_err = self:httpDownloadToFile(item_url, dest_path)
                        if dl_ok then
                            downloaded = downloaded + 1
                        else
                            logger.warn("DailyDigest: download failed:", filename, dl_err)
                            failed = failed + 1
                        end
                    end
                end
            end
        end

        local purged = self:purgeExpiredLocal()

        self.last_sync_ts = os.time()
        self.last_sync_date = os.date("%Y-%m-%d")
        self:saveSettings()

        if progress_msg then
            UIManager:close(progress_msg)
            progress_msg = nil
        end

        if downloaded > 0 or purged > 0 then
            self:refreshFileManagerIfNeeded()
        end

        if not is_auto or downloaded > 0 or purged > 0 then
            local summary = T(
                _("Daily Digest synced:\n%1 new, %2 purged"),
                downloaded,
                purged
            )
            if failed > 0 then
                summary = summary .. T(_(" (%1 failed)"), failed)
            end
            UIManager:show(InfoMessage:new{
                text = summary,
                timeout = 3,
            })
        end
    end)

    if progress_msg then
        UIManager:close(progress_msg)
    end
    self.syncing = false

    if not ok then
        logger.warn("DailyDigest: sync failed:", err)
        if not is_auto then
            UIManager:show(InfoMessage:new{
                text = T(_("Daily Digest sync failed:\n%1"), tostring(err)),
            })
        end
    end
end

return DailyDigest
