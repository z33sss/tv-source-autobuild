# TV 影视源自动体检 · AutoBuild Source

给 [iptvorganization/TV-AutoBuild](https://github.com/iptvorganization/TV-AutoBuild) 这类 **FongMi / TVBox 内核** 的影视 APK，提供一个**每天自动检测源是否失效、自动剔除死源、自动重新排序并发布**的接口文件。

你只需要把一个 URL 粘贴进 APK，剩下的交给 GitHub Actions。

```
config/sources.json   你维护的候选源（收养进来的站点/解析/直播）
        │
        ▼
scripts/autobuild.py  深度验证 → 更新历史 → 打分 → 排序 → 剔除连续失效源
        │
        ├── state/history.json    每个源的滚动健康档案（10 次窗口）
        ├── state/report.json     本次检测原始结果
        └── dist/tvbox.json       ★ 给 APK 用的接口文件
            dist/report.md        人读的日报
        │
        ▼
.github/workflows/daily.yml   每天定时跑 + commit + 发布到 GitHub Pages
```

---

## 一、这个 APK 要填什么地址

TV-AutoBuild 打包出来的是 FongMi 内核的 APK（`TV-mobile-*.apk` / `TV-leanback-*.apk`），
它读取的是标准 **TVBox / FongMi 接口 JSON**：

```json
{ "spider": "...", "sites": [...], "parses": [...], "lives": [...], "flags": [...] }
```

在 APK 里填地址的位置：

> 打开应用 → **设置** → **其他 / 接口** → **配置地址**（不同版本叫法略有差别，通常在「设置 → 其他 → 自定义接口地址」）→ 粘贴 URL → 确定。

可填的地址（部署完成后会得到）：

| 地址 | 说明 |
| --- | --- |
| `https://<你的用户名>.github.io/<仓库名>/tvbox.json` | **推荐**，走 GitHub Pages，国内通常可直连 |
| `https://raw.githubusercontent.com/<用户名>/<仓库名>/main/dist/tvbox.json` | 备用，部分地区可能被墙 |

> 想要更稳，也可以把 `dist/tvbox.json` 丢到任意 OSS / CF Workers 里再填那个地址。

---

## 二、快速开始

### 1. 建仓库并推上去

```bash
git clone <你的 fork 或本地目录>
cd tv-source-autobuild
git init && git add -A && git commit -m "init"
git remote add origin https://github.com/<你>/<仓库名>.git
git push -u origin main
```

### 2. 把你手上的源“收养”进来（自动适配）

如果你已经有一堆 TVBox 接口地址，不用手抄，直接导：

```bash
python scripts/import_sources.py https://example.com/tvbox.json
python scripts/import_sources.py ./backup.json --spider   # 同时采用它的 spider jar
python scripts/import_sources.py https://example.com/tvbox.json --dry-run   # 只看统计
```

它会解析 `sites` / `parses` / `lives`，按 `key` / `name` 去重后合并进 `config/sources.json`，
容忍尾逗号、`//` 与 `/* */` 注释、BOM。**只有能解析出真实探测地址的条目才会被收养**。

也可以直接手工编辑 `config/sources.json`（结构见下一节）。

### 3. 本地跑一次

```bash
python scripts/autobuild.py            # 检测 + 生成
python scripts/autobuild.py --check    # 只检测
python scripts/autobuild.py --build    # 只用上次的检测结果重新生成
```

产物：`dist/tvbox.json`（接口）、`dist/report.md`（日报）、`state/history.json`（健康档案）。

> 本机没有 Python 也可以：把仓库推上去后直接在 **Actions → Daily source check → Run workflow** 手动跑一次。

### 4. 打开自动化

1. **Settings → Actions → General → Workflow permissions** → 选 *Read and write permissions*（否则 commit 会被拒）。
2. **Settings → Pages → Source** → 选 **GitHub Actions**（否则 Pages 部署那步会报错）。
3. 推一次代码，`Actions` 里会自动出现 `Daily source check`，默认**每天 17:37 UTC（北京时间次日 01:37）**跑一次。

跑完后，`dist/tvbox.json` 会被 commit 并发布，`Actions` 的 **Summary** 页会直接显示当天的检测日报表格。

---

## 三、`config/sources.json` 结构

```jsonc
{
  "schema": 1,

  "settings": {
    "timeout": 10,          // 单次请求超时（秒）
    "workers": 8,           // 并发检测线程数
    "drop_after": 3,        // 连续失效 N 次才从输出里剔除（防抖动）
    "keyword": "中国",       // 采集站搜索验证用的关键词
    "parse_test_url": ""    // 填了就对解析接口做“真解出”深度验证
  },

  "app": {
    // 这一层会原样写进输出 JSON 的顶层，按需增删
    "spider": "",           // 公共爬虫 jar 地址
    "wallpaper": "...",
    "flags": ["qq", "qiyi", "youku", "..."],
    "doh": [ ... ],
    "ads": [],
    "hosts": {}
  },

  "sites":   [ ... ],       // 点播站点
  "parses":  [ ... ],       // 解析接口
  "lives":   [ ... ]        // 直播源
}
```

### 候选源里的“注释字段”

任何以 `_` 开头的字段（`_note`、`_comment`…）**只在候选池里有效，生成输出时会被自动剥掉**，
所以你可以放心给每个源写备注。

### 每个 `sites` 条目

就是标准的 TVBox/FongMi 站点对象，外加一个**可选**的 `probe`：

```jsonc
{
  "key": "demo",
  "name": "演示站",
  "type": 0,                          // 0/3 采集站, 1 规则站, 6 蜘蛛站
  "api": "https://x.com/api.php/provide/vod/",
  "ext": "...",                       // 字符串 URL 或 {"api": "..."}
  "jar": "https://x.com/custom_spider.jar",
  "searchable": 1, "quickSearch": 1, "filterable": 1, "changeable": 1,

  "_note": "写给自己的备注，不会进输出",

  "probe": {                          // 可选：不想让脚本猜就写死
    "kind": "cms",                    // cms | http | json | m3u8
    "url":  "https://x.com/api.php/provide/vod/"
  }
}
```

**没写 `probe` 时的自动推断：**

| 情况 | 探测方式 |
| --- | --- |
| `type` 为 0/3/6 且 `api` 是 URL | **采集站深度验证**：请求 `?ac=list&pg=1` → 要求返回非空条目且不是 HTML；再请求 `?ac=detail&wd=关键词` → 要求真能搜到东西 |
| `ext` 是 `.json` URL | 下载并要求能解析成带列表的 JSON |
| `ext` 是其它 URL / `jar` 是 URL | HTTP 探活 + 体积/内容校验（`jar` 太小视为失效） |
| `api` 是 `csp_Xxx` 这种类名、没有任何 URL | **标记为「无法验证」**，永不因检测而被剔除 |

> 换句话说：**深度验证**针对的是绝大多数的采集站（真正决定“源死没死”的那一层），
> 而不是只看首页 200。

### `parses` 与 `lives`

```jsonc
"parses": [{ "name": "示例解析", "type": 0, "url": "https://x.com/parse?url=" }]
"lives":  [{ "name": "示例直播", "type": 0, "url": "https://x.com/live.m3u8" }]
```

- `lives`：下载播放列表，要求 `#EXTM3U` 开头，统计频道数。
- `parses`：默认只做可达性探测；在 `settings.parse_test_url` 里填一个真实视频详情页地址后，
  会请求 `url + 该地址`，并要求返回的 JSON 里**真的解出了播放地址**。

---

## 四、“失效”是怎么判定的（防抖动设计）

一天的数据不作数，靠滚动历史：

| 状态 | 含义 | 是否进输出 |
| --- | --- | --- |
| `alive` | 本次探测通过 | ✅ 排最前 |
| `unverified` | 没有可探测地址（`csp_*` 类名站） | ✅ 其次 |
| `grace` | 本次失败，但连续失败次数 `< drop_after` | ✅ 排最后（暂时保留） |
| `dead` | 连续失败 `≥ drop_after`（默认 3 天） | ❌ 剔除 |

同一状态内按**得分**排序，得分由这些因素决定：

```
100
 - min(延迟ms / 100, 30)          越快越好
 + min(条目数, 20) * 0.5           目录越丰富越好
 - 最近 10 次的失败率 * 25          越稳定越好
 - 连续失败次数 * 10                越可靠越好
```

所以 `dist/tvbox.json` 里的顺序 = **又快、又全、又稳的源排在 APK 最前面**。

死掉的源不会从 `config/sources.json` 里删除——它只是被暂时踢出输出，
恢复后下一次检测会自动加回来。

---

## 五、每日流水线做什么

`.github/workflows/daily.yml`：

1. `cron: 37 17 * * *`（每天 17:37 UTC ≈ 北京时间次日 01:37）触发，也可手动 `Run workflow`。
2. 跑 `python scripts/autobuild.py`（深度检测 + 生成）。
3. 把 `state/` 和 `dist/` 的变化 commit 回仓库（带当天日报）。
4. 把 `dist/` 发布到 GitHub Pages → 你就有了稳定的 `tvbox.json` 地址。
5. 日报同时写进 Actions 的 **Summary**。

想要更高频？把 `cron` 改成每 6 小时一次即可（同一 repo 的 schedule 任务最少间隔 5 分钟，
但 GitHub 对活跃度低的仓库会自动降频，必要时用 `workflow_dispatch` 手动补跑）。

---

## 六、关于 Cloudflare

你也可以用 **Cloudflare Workers + Cron Triggers** 替代第 3、4 步：

- Worker 每天跑同样的 Python（Workers 用 `pyodide` 或改写成 JS），把结果写进 **KV**；
- 对外暴露 `https://<你的域名>/tvbox.json`，还可以顺带做缓存、UA 伪装、按地区分流。

好处是国内访问通常比 `raw.githubusercontent.com` 稳，坏处是多一套部署。
本仓库默认走 **GitHub Actions + Pages**（零额外部署），需要时再接 CF 即可。

---

## 七、常见问题

**Q：我一个源都没有，输出里全是 ❌？**
`config/sources.json` 里默认放的是**结构模板**（`*.example.com`）和两条**合法的公开 HLS 测试流**，
用来验证流水线本身是通的。用 `import_sources.py` 导入你自己的源，或手工替换掉模板条目。

**Q：为什么某些源第一天就被删了？**
不会。默认 `drop_after = 3`，第一次失败只进 `grace`。除非你的 `settings` 被改小了。

**Q：`csp_Xxx` 那种站为什么一直显示「无法验证」？**
它的可执行逻辑在公共 `jar` 里，没有独立 URL 可探。可以给它加一个 `probe` 指到站点首页或采集接口。

**Q：APK 里填了地址但不生效？**
先用浏览器打开你的 `tvbox.json` 确认能返回 JSON；再确认地址是 **https**、没有多余空格；
FongMi/TVBox 有些版本需要先在「设置 → 接口」里点「确认/保存」才会重载。

**Q：想强制立刻重检？**
Actions → `Daily source check` → `Run workflow` → 勾选 `force`。

---

## 八、免责声明

本仓库只是一个**通用的 URL 健康检查与配置生成工具**，本身不包含、不提供、不代理任何影视内容。
`config/sources.json` 中的条目需由使用者自行提供并自行承担合规责任。
请确保你所采集、聚合与分发的内容在你所在地区拥有合法授权，勿用于任何商业用途。
