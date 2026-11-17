# AgentEval 前端工作台

Vue 3 / TypeScript 工作台包含 Dashboard、数据集与用例、Agent 配置、实验与配置比较、评测详情、Badcase 和报告七个页面。完整首次运行步骤见 [运行手册](../docs/quickstart.md)，数据流和后台职责见 [架构说明](../docs/architecture.md)。

使用 Node.js `^20.19.0 || >=22.12.0`，从本目录执行：

```powershell
npm ci --ignore-scripts
npm run typecheck
npm run build
npm run dev
```

打开 [本机工作台](http://127.0.0.1:15173)。开发服务仅监听 `127.0.0.1`，端口为 `15173`；端口占用时明确退出。`/api` 默认代理到 `http://127.0.0.1:18080`，可在启动进程中修改：

```powershell
$env:AGENTEVAL_API_TARGET = 'http://127.0.0.1:18081'
npm run dev
```

`npm run preview` 用于本机预览构建产物，使用相同 API 代理。它不是带认证的公网部署方案。

刷新报告或详情地址后，页面会根据 URL 中的运行、用例和评分集身份重新读取已保存记录。实验页启动运行后按后端已提交事实显示进度；网络失败可重新查询，写操作不会因网络错误被自动重发。

API 类型位于 `src/types/index.ts`，请求与错误转换在 `src/api/index.ts`，路由在 `src/router/index.ts`。凭证字段只填写环境变量名，值由后端运行环境提供。后端错误保留安全错误码和追踪编号。

依赖精确版本见 `package.json` 和 `package-lock.json`。`npm run licenses` 从锁文件及安装包元数据生成 `dependency-licenses.json`，许可范围见 [第三方说明](../docs/third-party-notices.md)。
