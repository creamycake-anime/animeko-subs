# ani-subs

可用于 [Animeko](https://github.com/open-ani/animeko) 的数据源订阅。

## 用法

设置中添加订阅：

- `https://sub.creamycake.org/v1/css1.json`
- `https://sub.creamycake.org/v1/bt1.json`

支持 Ani 4.0.0 及以上版本。

### JSON API 数据源订阅

`v1/api1.json` 提供 JSON API 数据源，包含稀饭动漫 Next。该订阅需要支持
`json-api` v1 的 Animeko 客户端，依赖 [open-ani/animeko#3551](https://github.com/open-ani/animeko/pull/3551)。
旧版客户端不支持此数据源类型。正式兼容版本和上线时间以维护者发布为准。

该订阅单独生成，与现有 CSS Selector 和 BT 订阅分开。部署后可添加：
`https://sub.creamycake.org/v1/api1.json`。

稀饭 Next 的接口、公开前端密钥及验证说明见 [稀饭动漫 Next](docs/稀饭动漫-Next.md)。

## 维护数据源

每个 JSON 文件对应一个数据源。修改数据源时，直接编辑对应的单个 JSON 文件。

Web 数据源放在 `subs/web/t0/` 到 `subs/web/t4/`：

- `t0` 优先级最高，`t4` 优先级最低。
- 文件所在目录需要和 JSON 里的 `arguments.tier` 一致，例如 `arguments.tier` 为 `2` 的源放在 `subs/web/t2/`。
- 每个 Web 源都必须设置 `arguments.tier`。

BT 数据源放在 `subs/bt/`，例如 `subs/bt/AnimeGarden.json`。

JSON API 数据源放在 `subs/api/t0/` 到 `subs/api/t4/`，目录与 `arguments.tier` 一致，
例如 `subs/api/t0/稀饭动漫 Next.json`。

新增数据源时，使用数据源名称作为文件名。如果名称重复，文件名使用 `2`、`3` 后缀区分，例如 `XX数据源.json` 和 `XX数据源2.json`。
调整 Web 数据源的 tier 时，同时修改 JSON 里的 `arguments.tier` 并把文件移动到对应的 `tN` 目录。

数据源质量如何评测（实播指标口径、广告评判、线路能力分级 T0–T6、报告格式）见 [docs/数据源评测规范.md](docs/数据源评测规范.md)。

### 生成整合的数据源订阅文件

编辑完成后，运行：

```shell
node scripts/build-subs.js
```

脚本会生成 `v1/css1.json`、`v1/api1.json` 和 `v1/bt1.json` 供本地检查。`v1/` 是生成目录，不提交到仓库。
如果已经生成过 `v1/`，也可以检查当前源文件是否和生成结果一致：

```shell
node scripts/build-subs.js --check
```
