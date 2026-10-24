# Subscription wire format v1

All public JSON envelopes are `{ "payload": "base64(raw UTF-8 JSON)", "signature": "base64(Ed25519(raw bytes))" }`. The client's pinned public key is a 32-byte base64 Curve25519.Signing key. Verify the exact payload bytes before decoding; JSON canonicalization is only the publisher's deterministic serialization convention.

Profile `MTGA_CN_PROFILE_V1`: `feed`, `public_key`, `channel` (`stable` or `testing`). HTTPS directory URLs cannot contain user info, query or fragment. Absolute local feeds are explicit testing/local workflows. Installer-only `./feed` resolves relative to the supplied profile. The installed profile stores the resolved absolute local directory. Redirects must remain HTTPS and are bounded.

Index `MTGA_CN_INDEX_V1`: positive channel-wide `sequence`, ISO8601 UTC `expires_at`, `channel`, `releases`. Each reference binds release id, revision, exact game client_version/steam_build and manifest filename/sha256/bytes. Reject an expired index, lower sequence, duplicate identities, different bytes at a previously observed sequence, or clock rollback over five minutes. Local state deletion resets local replay history; this is not a hardware trust anchor.

Release `MTGA_CN_RELEASE_V1`: exact id/revision/version/build, minimum_client, platform `macOS-Steam`, architectures, publisher acceptance scope/evidence, five whitelisted resource rows, protected files. Each resource binds layer, game-relative path, input and output SHA256/bytes, and signed artifact fingerprint. Client rejects paths outside the built-in whitelist even if the publisher signs them. Stable requires accepted status and an evidence hash. A signature proves possession of the publisher key, not translation correctness or vendor authentication.

Delta `MTGADL01`:

- 8-byte ASCII magic, little-endian uint64 output bytes, uint32 instruction count.
- Copy: byte 0, uint64 source offset, uint32 length.
- Literal: byte 1, uint32 decompressed length, uint32 compressed length, zlib bytes.
- Instructions must exactly produce the declared output, exhaust input and match its SHA256. No execution or arbitrary file writes.
- At most 1,000,000 instructions; literal output at most 4 MiB, packed at most 8 MiB; delta at most 512 MiB; each resource at most 1 GiB. Copy bounds and decompression tails are checked.

Journal `MTGA_CN_SUBSCRIBER_TX_V1` binds UUID transaction, game path and exact build, release identity, protected files, per-layer before/after/original fingerprints. States: prepared → installing → installed; recovery: rolling-back → rolled-back. A rolled-back known before remains associated with its separately verified original base. Never infer an official base from a rollback file.

## GitHub 网络资源（客户端 v2）

当 `patch.url` 存在时，`minimum_client` 必须至少为 2。固定 feed 是
`https://raw.githubusercontent.com/<owner>/<repo>/main/feed`；差分 URL 仅接受相同 owner/repo 的
`https://github.com/<owner>/<repo>/releases/download/<tag>/<patch.name>`，不允许凭据、端口、查询、fragment 或路径编码歧义。HTTPS CDN 重定向由系统 curl 处理，下载内容最终按签名清单中的大小和 SHA-256 校验。

索引与 manifest 在 Git 中随同一提交切换；五个差分和安装 ZIP 先上传到独立草稿 Release，并通过认证 API 指纹（缺失指纹时认证完整下载）验证。索引成功切换并读回后才公开 Release 和通知订阅者，再检查匿名 HEAD（缺失指纹时匿名完整下载）。每个新修订独立 tag；已签名历史 tag 只按原引用续签，不新增附件。发布器冻结允许上传的文件，同名异内容拒绝覆盖，不使用 force push。此设计没有设置或确认 GitHub 服务端 immutable release。

联网包仅含 `mtga-cn`、`profile.json`、固定安装脚本、固定说明及 `SHA256.json`。ZIP 根目录必须是安全 basename，禁止额外成员、目录项、注释/extra metadata 和链接。每次发布均比对当前已审客户端、固定模板、订阅身份及完整文件清单。

## 协议 4：完整资源覆盖

资源行可含 `full: {name, fingerprint, url?}`。文件为未压缩的完整输出，名称以 `.full` 结束，fingerprint 必须等于该层 output；HTTPS 地址与差分使用相同仓库限制。发布包含 full 时 minimum_client 为4。客户端优先使用官方或已验证底稿的差分；无底稿时以完整输出覆盖未知补丁。before 记录实际当前文件，original_available=false，不伪造官方底稿；回滚仍恢复 before。受保护文件和活动路径校验保持有效。
