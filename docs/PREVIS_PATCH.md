# 预演补丁：把我们对 vendored DirectorDesk 的改动固化下来

`.runtime/vendor/director-desk/` 是原样 vendored 的上游预演前端（MIT 授权，`.runtime/` 在 gitignore 里）。
我们在它上面加了预演接口层（v1）与可读性层（v2），这些改动**只存在于那棵目录树里**：上游一升级，
整棵树被覆盖，改动要么丢失、要么凭记忆重做一遍。`patches/director-desk/<version>/` 把「我们改过 /
新增的文件」按上游版本存成**完整内容**，于是升级变成一次可核对、可重放的动作。补丁目录是我们自己的
代码，进 git；vendored 仓库永远只读。

改动的原因与接口契约不在这里：见 vendored 仓库里的 `PATCHES.md` 与 [PREVIS_HOST_API.md](PREVIS_HOST_API.md)。

## 补丁包含什么

当前补丁对应上游 `0.5.5+revision.1`（`package.json` 的 `version`；目录名用 `shortVersion`，即
`0.5.5.1`），10 个文件、约 180 KB：

| 文件 | 类型 | 内容 |
| --- | --- | --- |
| `src/host/host-api.ts` | 新增 | `window.__narrativePrevisHost` 接口本体：契约方法、离屏抓帧、`onChange`、`applyBatch` 事务 |
| `src/host/host-scene.ts` | 新增 | `Project` ↔ 契约 JSON 的翻译与每类写操作的校验 |
| `src/readability.ts` | 新增 | 可读性层：按 kind 配色、房间轮廓与尺寸、人物标志、3D 标签 |
| `src/engine.ts` | 修改 | 挂可读性层、`sample()` 人物着色、按视角切换可见性、标签字号与避让、`setReadability()` |
| `src/main.ts` | 修改 | `previsCommit()` 事务入口、`bindPrevisHostRuntime()`、标签字号渲染 |
| `src/startup.ts` | 修改 | 在三维引擎起来之前 `installPrevisHostApi()`，让 `hello()` 随时可回话 |
| `src/scenes/scene-workspace.ts` | 修改 | `commit()` 多收一个历史标签（撤销按钮的提示） |
| `src/storage.ts` | 修改 | `EditorHistory.commit(project, label?)` 跟上签名 |
| `package.json` | 修改 | 加 `pnpm.onlyBuiltDependencies: ["esbuild"]`，让 esbuild 能就地构建（不是行尾差异） |
| `PATCHES.md` | 新增 | 我们自己的改动清单与决定，随补丁一起带回来 |

没有改动主题、顶栏或样式表：顶栏压平与配色是**宿主侧**注入的（`src/narrative/previs_theme.py` +
`DirectorWorkspaceView.qml` 的主题脚本），本来就不在 vendored 里，所以也不在补丁里。

目录结构（`files/` 是仓库内相对路径的逐字节副本）：

```
patches/director-desk/0.5.5.1/
  manifest.json          # schema、上游版本、来源说明、每文件 sha256 与字节数
  files/src/engine.ts
  files/src/host/host-api.ts
  files/…                # 与仓库同构
```

## 四个子命令

```sh
python scripts/previs_patch.py snapshot   # 把改动导出成补丁目录
python scripts/previs_patch.py apply      # 把补丁目录写回 vendored 仓库
python scripts/previs_patch.py verify     # 比较仓库与补丁目录
python scripts/previs_patch.py build      # 重建 dist 并报告产物
```

默认仓库是 `.runtime/vendor/director-desk`，默认补丁根是 `patches/director-desk`；`--repo`、
`--patch`、`--patch-root` 可覆盖，`--out` 指定 snapshot 的输出目录。

| 子命令 | 做什么 | 关键参数 |
| --- | --- | --- |
| `snapshot` | 把被改动/新增的文件完整导出，写 `files/` 与 `manifest.json` | `--files`（省略则用 `git status` 自动找）、`--force` 覆盖非空目录、`--summary` 写来源说明 |
| `apply` | 校验版本 → 整体校验补丁哈希 → 覆盖前写 `文件.bak-<时间戳>` → 写入 | `--no-hint` 关掉下一步提示 |
| `verify` | 逐文件比对 sha256，输出「一致／缺失／被改动」，并核对上游版本 | 无 |
| `build` | 跑 `node node_modules/vite/bin/vite.js build`，打印 dist 文件数、总大小与最大 5 个产物 | `--node` 指定 node、`--dry-run` 只看命令与现有产物 |

退出码三个子命令统一：**0** 成功／一致；**1** 有漂移（缺失、被改动、版本不符）或构建失败；
**2** 拒绝执行或环境／用法错误（版本不符、补丁损坏或缺失、路径不合法、node/vite 找不到）。
`apply` 返回 **2** 时保证**一个文件都没写**。

## 升级上游的步骤

1. **拉新版**：用上游新版覆盖 `.runtime/vendor/director-desk`（`package.json` 的 `version` 会变）。
2. **看漂移**：`python scripts/previs_patch.py verify`。它按仓库版本去找补丁目录，版本不符会直接
   报 `[版本不符]` 并退出 1；你也能看到哪些文件被上游改掉了。
3. **套旧补丁？`apply` 会拒绝**（目标版本不同就不许写）。别绕过——那等于用旧实现覆盖上游新代码。
4. **手工重做**：以上一份补丁 `files/` 和 `PATCHES.md` 为参照，在新树里重做改动；`git diff` 看
   上游到底动了什么，接口壳（`src/host/*`）通常能整份搬过去，`engine.ts`／`main.ts` 需要按新版对齐。
5. **固化**：`python scripts/previs_patch.py snapshot`（可选 `--files`）生成 `patches/director-desk/<新版本>/`，
   旧版本目录保留，作为下一次升级的参照。
6. **构建**：`python scripts/previs_patch.py build`，然后冒烟（见 `PATCHES.md` 的 `previs_host_smoke.py`）。

## 已知风险

- **上游改名／重构会冲突**：补丁存的是“某版上游 + 我们的改动”的**整份文件**，不是 diff。上游把
  `engine.ts` 拆开、`main.ts` 挪函数、`Project` 结构变了，整份文件套上去就会抹掉上游的改动——
  所以只能手工重做，不能 `--force` 硬来。
- **只认版本字符串**：`version` 相同就当同一份上游。上游改了代码却没升版本（或我们自己改了版本号），
  `verify` 只会报文件漂移，不会替你判断该不该重做。
- **`PATCHES.md` 里那句“`package.json` 只是行尾差异”不准确**：它真实加了 5 行
  `pnpm.onlyBuiltDependencies`，所以 `package.json` 也在补丁里。vendored 仓库只读，改正留给下一次重做补丁时。
- **`dist/` 不在补丁里**：它是构建产物，`build` 现做；`apply` 之后必须重建，否则页面还是旧代码。
- **`node_modules` 不在补丁里**：只导出源码级文件，依赖树靠上游自带的那份。
- 补丁目录的 `manifest.json` 是脚本生成的，不要手改；手改后 `apply` 会因哈希不符拒绝写入。
