# FlowGrid Agent Memory 0.1.1 candidate

This is an uncommitted local candidate description, not a release announcement.
The working checkout can differ from the locally validated artifact. Installing
by package name does not bind this candidate.

中文说明：0.1.1 是本地候选；当前工作区、冻结候选包与正式发行属于不同对象。此文件不声明发布完成。

Candidate capabilities include the dedicated AML Add/Search transport,
source-bound build identity, the governed local SDK and optional lifecycle
reader/candidate-writer factories. Source reads keep raw evidence separate from
confirmed state. Explicit paired REST/MCP factories enable raw/candidate writes
only; they do not expose confirmation or administration through that mode.
Trusted identity, grant refresh, source lifecycle and cooperating locks remain
host responsibilities. Installing the modules activates no real host profile.

中文说明：新增能力保留证据与候选/确认状态区别。双工厂只允许原文和候选写入；真实身份、撤权和锁域仍由宿主接线，安装不自动启用真实 profile。

Local reproducible packaging, isolated installed tests, ARM64 container probes
and scoped SBOM coverage provide engineering evidence. SBOM coverage does not
establish complete dependency licenses, absence of vulnerabilities or signed
provenance. Self-generated acceptance/provenance JSON and Docker build metadata
are not independently verified GitHub Sigstore attestations. Such attestations
and the reviewed committed source remain release requirements, not artifacts
claimed to accompany this candidate.

中文说明：本地测试和物料清单仅证明已检查范围；未核验签名、全部许可证和漏洞。自生成 JSON 不能代替真实 CI 执行或签名证明。

The product remains alpha for one trusted local host. User IDs are logical
partitions, not hosted tenant-grade isolation. Public deployment, official AML
Smoke/Full, a new official score and real long-term task benefit remain unproven.
The inherited historical AML result is not a score for this candidate.

中文说明：尚未证明公网生产部署、新官方成绩或真实长期任务收益；历史成绩不移植到新版本。
