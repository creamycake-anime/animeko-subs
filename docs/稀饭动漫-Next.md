# 稀饭动漫 Next

配置文件：`subs/api/t0/稀饭动漫 Next.json`。生成订阅：`v1/api1.json`。

## 客户端要求

该数据源使用 `json-api` v1，依赖 [open-ani/animeko#3551](https://github.com/open-ani/animeko/pull/3551)
提供的通用 JSON API 数据源、配置编辑器和播放解析支持。需要包含该功能的客户端。
维护者发布支持版本并部署订阅后，用户可在数据源管理中添加 `api1.json` 订阅。

## 接口与凭据

| 功能 | POST 接口 |
| --- | --- |
| 搜索 | `https://api.xifanacg.com/rest/v1/rpc/search_animes` |
| 线路与剧集 | `https://api.xifanacg.com/rest/v1/rpc/get_anime_detail` |
| 播放解析 | `https://api.xifanacg.com/functions/v1/issue-web-playback` |

配置中的 `sb_publishable_` 密钥来自无需登录即可下载的官网前端 JavaScript，
不是用户账号令牌或服务端私密密钥。请求只携带公开前端请求头，不包含用户 Cookie 或登录会话。
2026-10-07 核对的前端文件为
`https://next.xifanacg.com/_next/static/chunks/05pvawgvkihwy.js`。

这些接口供网站前端使用，未确认有面向第三方的 API 文档或稳定性承诺。
接口、密钥或字段发生变化时，需要维护订阅配置。

## 解析与线路

- 搜索支持分页和原名、别名匹配。
- 详情读取全部非空线路及其剧集，以线路 `code` 作为播放请求的 `source`。
- 主集、SP、OVA 等类型保留各自的集数前缀。
- 播放时请求新的视频地址；优先选择与当前线路匹配的 `candidates`，保留稳定剧集标识。
- `arguments.tier` 和 `channelTiers` 是静态选择优先级；本配置未提供新一轮广告、画质或起播性能评级。

## 验证记录

2026-10-10 使用配置中的请求地址、请求体及公开请求头，未登录请求三部番剧：

| 搜索词 | 验证条目 | 非空线路 | 每条线路剧集数 |
| --- | --- | --- | --- |
| 葬送的芙莉莲 | 葬送的芙莉莲 | `xfy2`、`CS` | 28 |
| 胆大党 | 胆大党 | `xfxf1`、`AL`、`CS` | 12 |
| 进击的巨人 | 进击的巨人 第三季 | `xfy2` | 22 |

搜索、详情及上述每条线路第一集的播放解析均成功。该轮验证确认接口解析可用，
不等同于逐集实播或端到端画质、广告评测。

此前定制 Windows 客户端已通过 JSON 导入、条目搜索、线路/剧集枚举及真实播放验证，
用户确认实际播放正常；对 27 个条目的线路枚举与接口所有非空线路一致。
