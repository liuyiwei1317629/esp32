// =============================================================
// wifi_setup.cpp - Wi-Fi 配网 + bridge 地址
//
// 连网顺序：
//   1. NVS 里保存的账号（配网页面写进去的）
//   2. secrets.h 里的编译期账号（还没配过网时用它，保证烧完就能用）
//   3. 都连不上 -> 开热点 voice-ai-setup，手机连上去填 Wi-Fi 和密码
//
// 为什么要有第 3 步：换路由器 / 改密码 / 搬到别处，原来只能改 secrets.h
// 重新烧录。现在手机填一下就行，账号存在 NVS，掉电不丢。
// 密码明文存 NVS（没加密），风险等同于成对放在设备里，接受。
// =============================================================
#include "wifi_setup.h"
#include "config.h"

#if APP_MODE != APP_MODE_RECORD_ONLY
  #if __has_include("secrets.h")
    #include "secrets.h"
  #else
    #error "secrets.h not found: copy secrets.example.h to secrets.h and fill real values. Do NOT commit it."
  #endif
#else
  #define WIFI_SSID ""
  #define WIFI_PASS ""
  #define BRIDGE_HOST "0.0.0.0"
#endif

#if APP_MODE != APP_MODE_RECORD_ONLY

#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <ESPmDNS.h>
#include <Preferences.h>

static WebServer web(80);
static DNSServer dns;
static String    g_page;
static bool      g_portalOpen = false;

// ---------------- NVS ----------------
struct Creds { String ssid, pass, host; };

static bool loadCreds(Creds& c) {
  Preferences p;
  // 用读写方式打开：只读方式遇到"命名空间不存在"会打一行 nvs_open failed: NOT_FOUND，
  // 看着像出错，其实只是第一次上电。读写方式会顺手把空命名空间建出来。
  if (!p.begin("voice-ai", false)) return false;
  if (p.isKey("ssid")) c.ssid = p.getString("ssid", "");   // isKey 先问一遍，免得刷错误日志
  if (p.isKey("pass")) c.pass = p.getString("pass", "");
  if (p.isKey("host")) c.host = p.getString("host", "");
  p.end();
  return c.ssid.length() > 0;
}

static void saveCreds(const String& ssid, const String& pass, const String& host) {
  Preferences p;
  p.begin("voice-ai", false);
  p.putString("ssid", ssid);
  p.putString("pass", pass);
  if (host.length()) p.putString("host", host);
  else               p.remove("host");           // 清空 = 回到 secrets.h 的地址
  p.end();
}

// ---------------- mDNS ----------------
// 每次 IP 变了都重新注册，否则重连后 voice-ai.local 还指着旧地址。
// 重复 begin 会失败（mdns_init 只认第一次），所以先 end 再 begin。
// ⚠️ 改 Wi-Fi 模式（OFF/AP_STA）之前必须先 mdnsStop()：mDNS 记着旧的 netif 指针，
// 模式一变那个指针就失效，services 任务会 LoadProhibited 崩掉（实测崩过一次）。
static IPAddress g_mdnsIp(0, 0, 0, 0);

static void mdnsStop() {
  MDNS.end();
  g_mdnsIp = IPAddress(0, 0, 0, 0);
}

static void mdnsStart() {
  IPAddress ip = WiFi.localIP();
  if (g_mdnsIp == ip) return;
  if (g_mdnsIp != IPAddress(0, 0, 0, 0)) MDNS.end();
  if (MDNS.begin(MDNS_NAME)) {
    g_mdnsIp = ip;
    Serial.printf("mdns=%s.local ip=%s\n", MDNS_NAME, ip.toString().c_str());
  } else {
    Serial.println("mdns=begin_failed");
  }
}

// ---------------- 连一次 STA ----------------
static bool connectStation(const char* ssid, const char* pass, uint32_t timeout_ms, const char* src) {
  if (!ssid || !*ssid) return false;
  mdnsStop();             // 先停 mDNS，再动 Wi-Fi 模式（否则可能崩在 services 任务里）
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(MDNS_NAME);   // 路由器设备列表里也显示成 voice-ai
  WiFi.setSleep(false);          // 降低延迟
  WiFi.begin(ssid, (pass && *pass) ? pass : nullptr);   // 不打印密码
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED) {
    if (millis() - t0 > timeout_ms) {
      Serial.printf("wifi_fail src=%s ssid=%s ms=%lu\n", src, ssid,
                    (unsigned long)(millis() - t0));
      // 必须把这轮连接尝试停掉再回去：ESP32 停在 connecting 状态时，
      // 下一次 begin() 会被驱动拒绝（ESP_ERR_WIFI_STATE: sta is connecting），
      // 结果就是"第一次超时 -> 第二次永远连不上"（实测踩过）。
      WiFi.disconnect(true, false);
      delay(100);
      return false;
    }
    delay(100);
  }
  Serial.printf("wifi_ms=%lu ip=%s src=%s ssid=%s\n", (unsigned long)(millis() - t0),
                WiFi.localIP().toString().c_str(), src, ssid);
  mdnsStart();
  (void)wifiBridgeHost();   // 开机就把生效的 bridge 地址打出来，省得连不上时猜
  return true;
}

// ---------------- bridge 地址 ----------------
const char* wifiBridgeHost() {
  static bool   loaded = false;
  static String host;
  static char   resolved[20] = {0};
  static uint32_t resolvedAt = 0, triedAt = 0;

  if (!loaded) {
    loaded = true;
    Creds c;
    loadCreds(c);
    host = c.host.length() ? c.host : String(BRIDGE_HOST);
    Serial.printf("bridge_host=%s\n", host.c_str());
  }
  if (!host.endsWith(".local")) return host.c_str();

  // 主机名要先解析成 IP：lwIP 的 getaddrinfo 不认 mDNS，必须自己问。
  uint32_t now = millis();
  if (resolved[0] && now - resolvedAt < MDNS_REFRESH_MS) return resolved;
  if (triedAt && now - triedAt < MDNS_RETRY_MS) return resolved[0] ? resolved : host.c_str();
  triedAt = now;
  IPAddress ip = MDNS.queryHost(host.c_str(), 1200);
  if ((uint32_t)ip != 0) {
    snprintf(resolved, sizeof(resolved), "%u.%u.%u.%u", ip[0], ip[1], ip[2], ip[3]);
    resolvedAt = now;
    Serial.printf("bridge_host_resolve=%s -> %s\n", host.c_str(), resolved);
    return resolved;
  }
  Serial.printf("bridge_host_resolve_failed=%s\n", host.c_str());
  return resolved[0] ? resolved : host.c_str();   // 解析过就先用旧地址
}

// ---------------- 配网页面 ----------------
static String htmlEsc(const String& s) {
  String o;
  o.reserve(s.length() + 8);
  for (size_t i = 0; i < s.length(); i++) {
    char c = s[i];
    if (c == '&')       o += "&amp;";
    else if (c == '<')  o += "&lt;";
    else if (c == '>')  o += "&gt;";
    else if (c == '"')  o += "&quot;";
    else if (c == '\'') o += "&#39;";
    else                o += c;
  }
  return o;
}

static const char PAGE_TEMPLATE[] =
  "<!doctype html><html lang=zh><head><meta charset=utf-8>"
  "<meta name=viewport content=\"width=device-width,initial-scale=1\">"
  "<title>小鱼配网</title><style>"
  "body{font:16px/1.6 -apple-system,'Microsoft YaHei',sans-serif;margin:0;padding:16px;"
  "background:#f2f3f5;color:#111}"
  ".card{background:#fff;border-radius:12px;padding:18px;max-width:520px;margin:0 auto;"
  "box-shadow:0 1px 3px rgba(0,0,0,.12)}"
  "h1{font-size:20px;margin:0 0 6px}p{color:#555;margin:6px 0}"
  "label{display:block;margin:14px 0 6px;font-weight:600}"
  "input,select{width:100%;box-sizing:border-box;padding:12px;font-size:16px;"
  "border:1px solid #ccd0d5;border-radius:8px;background:#fff}"
  "button{width:100%;margin-top:20px;padding:14px;font-size:17px;border:0;border-radius:8px;"
  "background:#1f6feb;color:#fff;font-weight:600}"
  ".danger{background:#b42318;margin-top:10px;font-size:15px;padding:11px}"
  ".hint{font-size:13px;color:#888;margin-top:8px}"
  "</style></head><body><div class=card>"
  "<h1>小鱼配网</h1>"
  "<p>把小鱼连上你家 Wi-Fi（只支持 2.4 GHz，5G 连不上）。</p>"
  "<form method=post action=/save>"
  "<label>Wi-Fi 名</label>"
  "<input id=ssid name=ssid value=\"%SSID%\" placeholder=\"点下面的列表选，或直接输入\""
  " autocapitalize=off autocomplete=off spellcheck=false>"
  "%LIST%"
  "<label>Wi-Fi 密码</label>"
  "<input name=pass type=password placeholder=\"没密码就留空\" autocomplete=off>"
  "<label>电脑地址（bridge）</label>"
  "<input name=host value=\"%HOST%\" placeholder=\"192.168.0.101 或 电脑名.local\""
  " autocapitalize=off autocomplete=off spellcheck=false>"
  "<p class=hint>不改就沿用现在的。换了电脑或电脑 IP 变了才需要改。</p>"
  "<button type=submit>保存并重启</button>"
  "</form>"
  "<hr style=\"border:0;border-top:1px solid #e6e8eb;margin:18px 0\">"
  "<p class=hint>存错了、或者路由器密码改过：清空后小鱼回到出厂账号。</p>"
  "<form method=post action=/forget>"
  "<button class=danger type=submit>清空已保存的 Wi-Fi</button></form>"
  "<p class=hint>保存后小鱼会自己重启，这个叫 voice-ai-setup 的热点会消失。"
  "若一直连不上，用 USB 线接电脑、串口里敲 portal 回车，热点会重新出现。</p>"
  "</div></body></html>";

static const char PAGE_SAVED[] =
  "<!doctype html><html lang=zh><head><meta charset=utf-8>"
  "<meta name=viewport content=\"width=device-width,initial-scale=1\">"
  "<title>已保存</title><style>"
  "body{font:16px/1.6 -apple-system,'Microsoft YaHei',sans-serif;padding:24px;background:#f2f3f5}"
  ".card{background:#fff;border-radius:12px;padding:20px;max-width:520px;margin:0 auto}"
  "</style></head><body><div class=card><h1>已保存</h1>"
  "<p>小鱼正在重启并连接 Wi-Fi，大约 20 秒。这个页面可以关掉了。</p>"
  "<p class=hint>连上以后，按住录音键说话就行。</p></div></body></html>";

static const char PAGE_FORGOT[] =
  "<!doctype html><html lang=zh><head><meta charset=utf-8>"
  "<meta name=viewport content=\"width=device-width,initial-scale=1\">"
  "<title>已清空</title><style>"
  "body{font:16px/1.6 -apple-system,'Microsoft YaHei',sans-serif;padding:24px;background:#f2f3f5}"
  ".card{background:#fff;border-radius:12px;padding:20px;max-width:520px;margin:0 auto}"
  "</style></head><body><div class=card><h1>已清空</h1>"
  "<p>保存的 Wi-Fi 账号和电脑地址都删掉了，小鱼正在重启，回到出厂账号。</p>"
  "</div></body></html>";

static void buildPage() {
  Creds c;
  bool has = loadCreds(c);
  String curSsid = has && c.ssid.length() ? c.ssid : String(WIFI_SSID);
  String curHost = c.host.length() ? c.host : String(BRIDGE_HOST);

  String list;
  int n = WiFi.scanNetworks(false, false);
  int shown = 0;
  if (n > 0) {
    list += "<label>附近的网络</label><select onchange=\""
            "document.getElementById('ssid').value=this.value\">"
            "<option value=\"\">（不选，自己填上面）</option>";
    for (int i = 0; i < n && shown < 12; i++) {
      String s = WiFi.SSID(i);
      if (s.length() == 0) continue;
      list += "<option>"; list += htmlEsc(s); list += "</option>";
      shown++;
    }
    list += "</select>";
  } else {
    list += "<p class=hint>没扫到 Wi-Fi：可能路由器是 5 GHz，或离得太远。可以直接手填。</p>";
  }
  WiFi.scanDelete();

  String page = PAGE_TEMPLATE;
  page.replace("%SSID%", htmlEsc(curSsid));
  page.replace("%HOST%", htmlEsc(curHost));
  page.replace("%LIST%", list);
  g_page = page;
}

static void handleSave() {
  String ssid = web.arg("ssid"); ssid.trim();
  String pass = web.arg("pass");
  String host = web.arg("host"); host.trim();
  if (ssid.length() == 0) {
    web.send(400, "text/plain; charset=utf-8", "Wi-Fi 名不能为空");
    return;
  }
  if (ssid.length() > 32 || pass.length() > 64 || host.length() > 64) {
    web.send(400, "text/plain; charset=utf-8", "内容过长，请检查");
    return;
  }
  if (host.indexOf(' ') >= 0) {
    web.send(400, "text/plain; charset=utf-8", "电脑地址里不能有空格");
    return;
  }
  saveCreds(ssid, pass, host);
  Serial.printf("wifi_portal=saved ssid=%s host=%s\n", ssid.c_str(),
                host.length() ? host.c_str() : "(unchanged)");
  web.send(200, "text/html; charset=utf-8", PAGE_SAVED);
  web.client().flush();
  delay(1200);
  ESP.restart();
}

static void startPortal() {
  // 注册只做一次：WebServer 的 on() 是往链表里追加，重复开会越堆越多
  static bool handlersReady = false;
  if (!handlersReady) {
    handlersReady = true;
    web.on("/", HTTP_GET, []() {
      web.send(200, "text/html; charset=utf-8", g_page);
    });
    web.on("/save", HTTP_POST, handleSave);
    // 存错了的退路：清掉 NVS 回到 secrets.h 的出厂账号
    web.on("/forget", HTTP_POST, []() {
      wifiSetupForget();
      web.send(200, "text/html; charset=utf-8", PAGE_FORGOT);
      web.client().flush();
      delay(1200);
      ESP.restart();
    });
    // 手机/电脑的联网探测（/generate_204、/ncsi.txt 等）一律引到配网页，
    // 这样一join热点就会自动弹页面，不用手输 192.168.4.1。
    web.onNotFound([]() {
      web.sendHeader("Location", String("http://") + WiFi.softAPIP().toString() + "/", true);
      web.send(302, "text/plain", "");
    });
  }

  mdnsStop();
  // 已经连上网时（串口手动开 portal）保留 STA：修配网不该把当前连接掐掉。
  bool hadSta = (WiFi.status() == WL_CONNECTED);
  if (!hadSta) WiFi.disconnect(true, false);
  WiFi.mode(WIFI_AP_STA);        // AP_STA：开热点，同时还能扫网络
  WiFi.setSleep(false);
  if (!WiFi.softAP(PORTAL_AP_SSID)) {   // 开放热点，不设密码
    Serial.println("wifi_portal=ap_failed");
    return;
  }
  dns.setErrorReplyCode(DNSReplyCode::NoError);
  if (!dns.start(53, "*", WiFi.softAPIP())) Serial.println("wifi_portal=dns_failed");
  buildPage();
  web.begin();

  IPAddress apip = WiFi.softAPIP();
  Serial.printf("wifi_portal=on ssid=%s ip=%s\n", PORTAL_AP_SSID, apip.toString().c_str());

  g_portalOpen = true;
  uint32_t t0 = millis(), lastClient = t0;
  uint8_t clients = 0;
  for (;;) {
    dns.processNextRequest();
    web.handleClient();
    wifiSetupPollSerial();   // 热点开着时串口照样能敲 forget，不然错账号只能拆机重烧
    uint32_t now = millis();
    uint8_t n = WiFi.softAPgetStationNum();
    if (n != clients) {
      clients = n;
      lastClient = now;
      Serial.printf("wifi_portal=clients %u\n", (unsigned)n);
    }
    // 有人连着就等，一直没人来就早点收工，别把录音键卡死 10 分钟
    if (now - t0 > PORTAL_MAX_MS) break;
    if (clients == 0 && now - lastClient > PORTAL_IDLE_MS) break;
    delay(2);
  }
  g_portalOpen = false;

  Serial.println("wifi_portal=timeout");
  web.stop();
  dns.stop();
  WiFi.softAPdisconnect(true);
  if (hadSta && WiFi.status() == WL_CONNECTED) {
    WiFi.mode(WIFI_STA);   // 手动开的热点关掉，原来的网还在
    mdnsStart();
  } else {
    WiFi.mode(WIFI_OFF);
  }
}

// ---------------- 对外 ----------------
bool wifiSetupEnsure() {
  if (WiFi.status() == WL_CONNECTED) return true;

  Creds c;
  bool hasNvs = loadCreds(c);
  if (hasNvs && connectStation(c.ssid.c_str(), c.pass.c_str(), WIFI_CONNECT_TIMEOUT_MS, "nvs")) {
    return true;
  }
  // 上次配的网连不上（换地方了/改密码了）就试编译期账号，两条都废才开热点
  if (connectStation(WIFI_SSID, WIFI_PASS, WIFI_CONNECT_TIMEOUT_MS, "secrets")) {
    // 配过网却连不上 = NVS 里那组是坏的（比如密码打错）。串口 / portal 页面都能清。
    if (hasNvs) Serial.printf("wifi_warn=saved_creds_failed ssid=%s\n", c.ssid.c_str());
    return true;
  }

  startPortal();   // 保存成功会 ESP.restart()，回不到这里
  return false;
}

void wifiSetupForcePortal() { startPortal(); }

void wifiSetupForget() {
  Preferences p;
  if (p.begin("voice-ai", false)) {
    p.clear();
    p.end();
  }
  Serial.println("wifi_forget=ok");
}

// 串口命令：portal = 开配网热点，forget = 清掉 NVS 里的账号。
// 不动按键语义（BOOT 始终是"按住说话"）；配网热点开着时也照常受理。
void wifiSetupPollSerial() {
  static char buf[16];
  static size_t n = 0;
  while (Serial.available()) {
    char ch = (char)Serial.read();
    if (ch == '\r' || ch == '\n') {
      buf[n] = '\0';
      if (n && strcmp(buf, "portal") == 0) {
        Serial.println("portal_cmd");
        if (!g_portalOpen) startPortal();
      } else if (n && strcmp(buf, "forget") == 0) {
        wifiSetupForget();
      }
      n = 0;
    } else if (n < sizeof(buf) - 1) {
      buf[n++] = ch;
    }
  }
}

#else   // RECORD_ONLY 不联网
bool wifiSetupEnsure() { return false; }
void wifiSetupForcePortal() {}
void wifiSetupForget() {}
void wifiSetupPollSerial() {}
const char* wifiBridgeHost() { return BRIDGE_HOST; }
#endif
