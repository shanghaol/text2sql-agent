package com.shanghl.text2sql.service;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.UUID;

import javax.crypto.AEADBadTagException;

import com.baomidou.mybatisplus.core.toolkit.Wrappers;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.shanghl.text2sql.common.BizException;
import com.shanghl.text2sql.common.ErrorCode;
import com.shanghl.text2sql.config.AppProperties;
import com.shanghl.text2sql.dto.ApprovalRequest;
import com.shanghl.text2sql.entity.ApiKey;
import com.shanghl.text2sql.entity.QueryHistory;
import com.shanghl.text2sql.entity.User;
import com.shanghl.text2sql.mapper.ApiKeyMapper;
import com.shanghl.text2sql.mapper.QueryHistoryMapper;
import com.shanghl.text2sql.mapper.UserMapper;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

/**
 * POST /api/chat 的 SSE 代理（设计 §2.5 六步，~150 行核心）：
 *
 *   ① JWT 已过（JwtAuthFilter）→ 限流检查（10/min/用户）→ 并发闸 incrementAndGet &gt; 4 → 503
 *      （ChatSession.close 的 finally 里 decrementAndGet，流异常也要放闸）；
 *   ② 读 api_key 行：无行 → 403 key_not_set；有行 → AES-GCM 解密（微秒级，不缓存明文，§1.3）；
 *   ③ JDK HttpClient POST {python}/agent/chat，头三件 X-User-LLM-Key / X-User-Id / X-Internal-Token，
 *      body {question, threadId}；连接超时 5s（HttpClientConfig），读超时不设（§2.5 步骤 3）；
 *   ④ 上游非 200：读 JSON 错误体 → 转发状态与业务码（流前错误走这条路）；
 *   ⑤ 上游 200：ofInputStream → BufferedReader 逐行读、逐行写 + 每空行 flush（SSE 以空行分事件）；
 *      同时旁路解析 delta/tool/done/error（解析绝不影响透传）；
 *   ⑥ 流结束（done / error / 客户端断连 / 异常）→ 写 query_history（status 1/2/3，W5 增 4=待审批）→
 *      向流内注入 saved 事件（写库失败注入 historyId:null + ERROR 日志，不弄断已收完的回答）→ 放闸。
 *
 * W5 HITL 增量（设计 §3.4/§3.6）：
 *   - handleData 识别 pending_approval 事件（Python 停在 interrupt）→ 流结束按 status=4 落库
 *     → 注入 pending_saved {historyId}（前端绑进审批卡）；
 *   - openApproval + streamAndPersistForApproval：/api/chat/approval 的转发与收场——
 *     落库从 INSERT 改为 UPDATE（读原 status=4 行，累加 latency/tokens，approve→1 / reject→3）。
 *
 * 写路径归属论证（§2.5 表格）：历史写入是每轮必然发生的确定性副作用，绝不经过模型——
 * 模型可以漏调错调，且写入参数含模型的最终回答全文，工具调用时机上根本拿不到。
 *
 * 日志纪律（§1.5 规则 1/2/7/9）：日志只出现 keyFp=指纹 与 keyLen=长度；SSE 下行不回显 key；
 * error 事件只透传业务码与可读 message。
 */
@Service
public class ChatProxyService {

    private static final Logger log = LoggerFactory.getLogger(ChatProxyService.class);

    /** reject 决策的落库语义码（W5 §3.4：status=3 用户中断族 + 专用 error_code）。 */
    static final String ERROR_APPROVAL_REJECTED = "approval_rejected";

    private final ApiKeyMapper apiKeyMapper;
    private final QueryHistoryMapper historyMapper;
    private final UserMapper userMapper;
    private final KeyCipherService cipher;
    private final RateLimiter rateLimiter;
    private final ConcurrencyGate gate;
    private final HttpClient httpClient;
    private final ObjectMapper objectMapper;
    private final AppProperties props;

    public ChatProxyService(ApiKeyMapper apiKeyMapper, QueryHistoryMapper historyMapper, UserMapper userMapper,
                            KeyCipherService cipher, RateLimiter rateLimiter, ConcurrencyGate gate,
                            HttpClient httpClient, ObjectMapper objectMapper, AppProperties props) {
        this.apiKeyMapper = apiKeyMapper;
        this.historyMapper = historyMapper;
        this.userMapper = userMapper;
        this.cipher = cipher;
        this.rateLimiter = rateLimiter;
        this.gate = gate;
        this.httpClient = httpClient;
        this.objectMapper = objectMapper;
        this.props = props;
    }

    /** ①-④：前置校验 + 读 key + 解密 + 建立上游流。成功返回 ChatSession（占用并发闸，直到 close）。 */
    public ChatSession open(long userId, String question, String threadId) {
        validateThreadId(threadId); // 422 bad_request（§3.3）
        // ① 限流（10/min/用户，固定窗口）→ 并发闸
        rateLimiter.check("chat:" + userId, ErrorCode.RATE_LIMITED,
                props.getChat().getRateLimitPerMin(), 60_000L);
        if (!gate.tryEnter()) {
            throw new BizException(ErrorCode.CONCURRENCY_FULL);
        }
        HttpResponse<InputStream> upstream = null;
        try {
            // ② 读 key 行（无行 → 403 key_not_set）→ 解密
            ApiKey row = apiKeyMapper.selectOne(
                    Wrappers.<ApiKey>lambdaQuery().eq(ApiKey::getUserId, userId));
            if (row == null) {
                throw new BizException(ErrorCode.KEY_NOT_SET);
            }
            String plaintext;
            try {
                plaintext = cipher.decrypt(row.getKeyIv(), row.getKeyCiphertext()); // 显式解密点
            } catch (AEADBadTagException e) {
                // 主密钥不匹配（换环境忘迁 BYOK_MASTER_KEY）→ 引导联系管理员（§1.3）
                log.error("decrypt failed userId={} fp={}", userId, row.getKeyFingerprint());
                throw new BizException(ErrorCode.MASTER_KEY_MISMATCH);
            }
            // §1.5 规则 1/2：不打请求体；日志替身 = 指纹 + 长度
            log.info("chat start userId={} threadId={} keyFp={} keyLen={}",
                    userId, threadId, row.getKeyFingerprint(), plaintext.length());

            // ③ 建上游请求。key 只走头与体，绝不进 URL（规则 3：URL 会进访问日志与浏览器历史）
            String jsonBody = objectMapper.writeValueAsString(
                    Map.of("question", question, "threadId", threadId));
            HttpRequest.Builder builder = HttpRequest.newBuilder(
                            URI.create(props.getPython().getBaseUrl() + "/agent/chat"))
                    .header("Content-Type", "application/json")
                    .header("X-User-LLM-Key", plaintext)
                    .header("X-User-Id", String.valueOf(userId))        // Python 据此做 thread 命名空间（u{userId}:）
                    .header("X-Internal-Token",
                            props.getInternalToken() == null ? "" : props.getInternalToken());
            // W5 §3.8：开关是请求域的——按用户行 hitl_enabled 注入 X-User-HITL: 1（仅开启时）
            if (hitlEnabled(userId)) {
                builder.header("X-User-HITL", "1");
            }
            HttpRequest request = builder
                    .POST(HttpRequest.BodyPublishers.ofString(jsonBody, StandardCharsets.UTF_8))
                    .build();
            // 请求级【不设】timeout：SSE 流可能静默数十秒（§2.5 步骤 3）；连接超时在 HttpClient 上（5s）
            upstream = httpClient.send(request, HttpResponse.BodyHandlers.ofInputStream());

            // ④ 上游非 200：读 JSON 错误体 → 转发（流前错误）
            if (upstream.statusCode() != 200) {
                throw mapUpstreamError(upstream);
            }
            return new ChatSession(userId, threadId, question, System.currentTimeMillis(), upstream, gate, null);
        } catch (IOException e) {
            closeQuietly(upstream);
            gate.exit(); // 流异常也要放闸（§2.5 步骤 1）
            log.warn("upstream unreachable userId={}: {}", userId, e.getClass().getSimpleName());
            throw new BizException(ErrorCode.UPSTREAM_UNREACHABLE,
                    "上游服务暂时不可用（Python 未启动或网络不通），请稍后重试");
        } catch (InterruptedException e) {
            closeQuietly(upstream);
            gate.exit();
            Thread.currentThread().interrupt();
            throw new BizException(ErrorCode.UPSTREAM_UNREACHABLE);
        } catch (RuntimeException e) {
            closeQuietly(upstream);
            gate.exit();
            throw e;
        }
    }

    /** W5 §3.6：读用户行的 HITL 开关（PK 单查微秒级）；缺行/未配置一律按关处理。 */
    private boolean hitlEnabled(long userId) {
        User user = userMapper.selectById(userId);
        return user != null && Integer.valueOf(1).equals(user.getHitlEnabled());
    }

    /**
     * W5 §3.4/§3.6：审批恢复请求的前置链——限流/并发闸/原行校验/读 key 解密/连上游。
     * 与 open() 的差异：转发目标 /agent/chat/approval、恒带 X-User-HITL: 1（resume 轮必开）、
     * 额外做审批原行的归属（越权红线 §1.7）、状态（幂等）与 threadId 一致性（W7 §3.2）
     * 校验，行随 ChatSession 带到落库段。
     */
    public ChatSession openApproval(long userId, ApprovalRequest req) {
        validateThreadId(req.threadId());
        if (!"approve".equals(req.decision()) && !"reject".equals(req.decision())) {
            throw new BizException(ErrorCode.APPROVAL_BAD_DECISION); // @Valid 正则外的服务端白名单兜底
        }
        // ① 限流（与对话同桶：审批是同一轮的延续）→ 并发闸（恢复轮是一次真实 LLM 调用）
        rateLimiter.check("chat:" + userId, ErrorCode.RATE_LIMITED,
                props.getChat().getRateLimitPerMin(), 60_000L);
        if (!gate.tryEnter()) {
            throw new BizException(ErrorCode.CONCURRENCY_FULL);
        }
        HttpResponse<InputStream> upstream = null;
        try {
            // ② 审批原行：归属 + 状态 + threadId 交叉校验（W5 §3.6 streamAndPersistForApproval 的 UPDATE 目标）
            QueryHistory original = historyMapper.selectById(req.historyId());
            if (original == null || original.getUserId() == null || original.getUserId() != userId) {
                throw new BizException(ErrorCode.HISTORY_NOT_FOUND); // 非本人行一律 404，不泄露存在性
            }
            if (original.getStatus() == null || original.getStatus() != QueryHistory.STATUS_PENDING) {
                throw new BizException(ErrorCode.APPROVAL_NOT_FOUND); // 幂等：该行已不在待审批态
            }
            // W7 §3.2：threadId 交叉校验——同用户把 A 会话的审批请求配上 B 会话的 threadId
            // 一并拒绝（resume 会打到 B 会话的 LangGraph 线程，破坏会话一致性）。忽略大小写
            // 比较（UUID 十六进制大小写无语义差异，两侧都已过 validateThreadId 的 UUID 解析，
            // §8-9）；错误码复用 APPROVAL_NOT_FOUND 409——与"行不在待审批态"同码，
            // 不泄露该行是否存在于其他会话（越权面一致性）。
            if (!req.threadId().equalsIgnoreCase(original.getThreadId())) {
                throw new BizException(ErrorCode.APPROVAL_NOT_FOUND);
            }
            // ③ 读 key 行 + 解密（与 open() 同款，微秒级、不缓存明文）
            ApiKey row = apiKeyMapper.selectOne(
                    Wrappers.<ApiKey>lambdaQuery().eq(ApiKey::getUserId, userId));
            if (row == null) {
                throw new BizException(ErrorCode.KEY_NOT_SET);
            }
            String plaintext;
            try {
                plaintext = cipher.decrypt(row.getKeyIv(), row.getKeyCiphertext());
            } catch (AEADBadTagException e) {
                log.error("decrypt failed userId={} fp={}", userId, row.getKeyFingerprint());
                throw new BizException(ErrorCode.MASTER_KEY_MISMATCH);
            }
            log.info("approval start userId={} threadId={} historyId={} decision={} keyFp={}",
                    userId, req.threadId(), req.historyId(), req.decision(), row.getKeyFingerprint());

            // ④ 转发 Python /agent/chat/approval（头四件；resume 轮恒开 HITL）
            String jsonBody = objectMapper.writeValueAsString(Map.of(
                    "threadId", req.threadId(),
                    "approvalId", req.approvalId(),
                    "decision", req.decision()));
            HttpRequest request = HttpRequest.newBuilder(
                            URI.create(props.getPython().getBaseUrl() + "/agent/chat/approval"))
                    .header("Content-Type", "application/json")
                    .header("X-User-LLM-Key", plaintext)
                    .header("X-User-Id", String.valueOf(userId))
                    .header("X-Internal-Token",
                            props.getInternalToken() == null ? "" : props.getInternalToken())
                    .header("X-User-HITL", "1")
                    .POST(HttpRequest.BodyPublishers.ofString(jsonBody, StandardCharsets.UTF_8))
                    .build();
            upstream = httpClient.send(request, HttpResponse.BodyHandlers.ofInputStream());

            if (upstream.statusCode() != 200) {
                throw mapUpstreamError(upstream);
            }
            return new ChatSession(userId, req.threadId(), original.getQuestion(),
                    System.currentTimeMillis(), upstream, gate, original);
        } catch (IOException e) {
            closeQuietly(upstream);
            gate.exit();
            log.warn("upstream unreachable userId={}: {}", userId, e.getClass().getSimpleName());
            throw new BizException(ErrorCode.UPSTREAM_UNREACHABLE,
                    "上游服务暂时不可用（Python 未启动或网络不通），请稍后重试");
        } catch (InterruptedException e) {
            closeQuietly(upstream);
            gate.exit();
            Thread.currentThread().interrupt();
            throw new BizException(ErrorCode.UPSTREAM_UNREACHABLE);
        } catch (RuntimeException e) {
            closeQuietly(upstream);
            gate.exit();
            throw e;
        }
    }

    /** ⑤-⑥：透传 + 旁路解析 + 落库 + saved 注入。结束后 close（放并发闸）。 */
    public void streamAndPersist(ChatSession session, SseSink sink) {
        StreamContext ctx = new StreamContext();
        TransferOutcome outcome = transfer(session, ctx, sink);
        if (outcome.upstreamDropped() && ctx.errorCode == null) {
            ctx.errorCode = "llm_upstream_error";
        }
        long latencyMs = ctx.latencyMs != null ? ctx.latencyMs
                : System.currentTimeMillis() - session.startMs();
        int status;
        if (ctx.pending) {
            status = QueryHistory.STATUS_PENDING; // W5 §3.6：停在 interrupt 等审批（done 缺席是停车信号之一）
        } else if (outcome.clientAborted()) {
            status = QueryHistory.STATUS_ABORTED;
        } else if (ctx.errorCode != null) {
            status = QueryHistory.STATUS_FAILED;
        } else {
            status = QueryHistory.STATUS_SUCCESS;
        }

        try {
            // ⑥ 流结束 → 写 query_history → 注入 saved / pending_saved（W5 §3.4）
            Long historyId = persistHistory(session, ctx, status, latencyMs);
            if (ctx.pending) {
                writePendingSaved(sink, historyId); // 前端把 historyId 绑进审批卡，批准后原地 UPDATE
            } else {
                writeSaved(sink, historyId);
            }
            log.info("chat finished userId={} threadId={} status={} historyId={} latencyMs={}",
                    session.userId(), session.threadId(), status, historyId, latencyMs);
            try {
                sink.flush();
            } catch (IOException ignore) {
                // 客户端已断开，冲刷失败无碍
            }
        } finally {
            session.close(); // 放并发闸 + 关上游流（无论哪种收场）
        }
    }

    /** W5 §3.4 [批准/拒绝]：审批恢复流的透传 + 原地 UPDATE 落库 + saved 注入。
     *  与 streamAndPersist 的差异只在收场：INSERT 改 UPDATE（读原行累加 latency/tokens），
     *  answer_text 取两段之和（停车轮前言 + 恢复轮作答，与前端气泡的续写结果一致）。 */
    public void streamAndPersistForApproval(ChatSession session, SseSink sink, String decision) {
        StreamContext ctx = new StreamContext();
        TransferOutcome outcome = transfer(session, ctx, sink);
        if (outcome.upstreamDropped() && ctx.errorCode == null) {
            ctx.errorCode = "llm_upstream_error";
        }
        long wallLatencyMs = System.currentTimeMillis() - session.startMs();
        QueryHistory original = session.pendingHistory();
        try {
            int status = updateApprovalHistory(ctx, original, decision, outcome.clientAborted(), wallLatencyMs);
            if (ctx.pending) {
                // W5 §8-⑰ 链式审批：恢复轮又停在新 interrupt（批准第 1 条后模型为第 2 条 SQL
                // 重新发起）——行保持 status=4，再注入 pending_saved 续接下一张审批卡
                writePendingSaved(sink, original.getId());
            } else {
                writeSaved(sink, original.getId()); // 复用 saved 事件：前端续写气泡 + 反馈按钮解锁
            }
            log.info("approval finished userId={} threadId={} decision={} historyId={} status={} latencyMs={} tokenIn={} tokenOut={}",
                    session.userId(), session.threadId(), decision, original.getId(), status,
                    original.getLatencyMs(), original.getTokenIn(), original.getTokenOut());
            try {
                sink.flush();
            } catch (IOException ignore) {
                // 客户端已断开，冲刷失败无碍
            }
        } finally {
            session.close();
        }
    }

    // ------------------------------------------------------------------ 透传公共段

    /** ⑤ 透传 + 旁路解析公共循环（/api/chat 与 /api/chat/approval 共用）：
     *  逐行读、逐行写、每空行 flush；旁路解析失败只跳行绝不影响透传。 */
    private TransferOutcome transfer(ChatSession session, StreamContext ctx, SseSink sink) {
        boolean clientAborted = false;
        boolean upstreamDropped = false;
        try {
            BufferedReader reader = new BufferedReader(
                    new InputStreamReader(session.upstream().body(), StandardCharsets.UTF_8));
            String line;
            while ((line = reader.readLine()) != null) { // 逐行读（§2.5 步骤 5）
                try {
                    parseLine(line, ctx); // 旁路解析：解析失败只跳过该行，绝不影响透传
                } catch (Exception pe) {
                    log.debug("sse bypass parse skipped: {}", pe.toString());
                }
                try {
                    sink.writeLine(line); // 逐行写（原文透传）
                    if (line.isEmpty()) {
                        sink.flush(); // 空行 = 事件边界 → 立即冲刷，delta 到达即渲染（§9.2-7）
                    }
                } catch (IOException io) {
                    // ClientAbortException（IOException 子类）→ 客户端中途断连 → status=3
                    clientAborted = true;
                    log.info("client aborted userId={} threadId={}（按 status=3 落库）",
                            session.userId(), session.threadId());
                    break; // 停止读上游；close() 关流 → HTTP 层传导断开到 Python（§9.2-5）
                }
            }
        } catch (IOException e) {
            // 上游中途断流（非客户端原因）→ 按失败落库
            upstreamDropped = true;
            log.warn("upstream stream dropped userId={} threadId={}: {}",
                    session.userId(), session.threadId(), e.getClass().getSimpleName());
        }
        return new TransferOutcome(clientAborted, upstreamDropped);
    }

    private record TransferOutcome(boolean clientAborted, boolean upstreamDropped) {
    }

    // ------------------------------------------------------------------ 旁路解析

    /** 单行旁路解析：只认 event:/data: 字段；注释行与其他行仅透传。 */
    private void parseLine(String line, StreamContext ctx) throws IOException {
        if (line.startsWith("event:")) {
            ctx.currentEvent = trimField(line.substring("event:".length()));
        } else if (line.startsWith("data:")) {
            String data = trimField(line.substring("data:".length()));
            if (!data.isEmpty()) {
                handleData(ctx.currentEvent, data, ctx);
            }
        }
    }

    private void handleData(String event, String data, StreamContext ctx) throws IOException {
        JsonNode node = objectMapper.readTree(data);
        String name = event == null ? "message" : event;
        switch (name) {
            case "delta" -> ctx.answer.append(node.path("text").asText("")); // 累加 answer_text
            case "tool" -> {
                String tool = node.path("name").asText(null);
                if (tool != null) {
                    ctx.lastTool = tool; // 记最后工具名
                }
                // 前瞻兼容：§3.2 契约 tool 事件只有 name；若 T2 携带 sql 字段（最后一次
                // execute_readonly_sql 的 SQL），在此捕获供 query_history.generated_sql（W5 消费）
                JsonNode sql = node.get("sql");
                if (sql != null && !sql.isNull() && !sql.asText().isBlank()) {
                    ctx.generatedSql = sql.asText();
                }
            }
            case "done" -> {
                JsonNode tokens = node.get("tokens");
                if (tokens != null) {
                    ctx.tokenIn = intOrNull(tokens.get("input"));
                    ctx.tokenOut = intOrNull(tokens.get("output"));
                }
                ctx.latencyMs = intOrNull(node.get("latencyMs"));
            }
            case "pending_approval" -> {
                // W5 §3.4：Python 停在 interrupt——记 pending + SQL（tool 事件已抓，此处冗余兜底）。
                // latencyMs/tokens 为 additive 字段：停车轮已消耗的部分随 status=4 行落库，
                // approve 的 UPDATE 两段累加后 total 才完整（§3.6）。
                ctx.pending = true;
                JsonNode sql = node.get("sql");
                if (sql != null && !sql.isNull() && !sql.asText().isBlank()) {
                    ctx.generatedSql = sql.asText();
                }
                JsonNode tokens = node.get("tokens");
                if (tokens != null) {
                    ctx.tokenIn = intOrNull(tokens.get("input"));
                    ctx.tokenOut = intOrNull(tokens.get("output"));
                }
                Integer pendingLatency = intOrNull(node.get("latencyMs"));
                if (pendingLatency != null) {
                    ctx.latencyMs = pendingLatency;
                }
            }
            case "error" -> {
                // §1.5 规则 7：error 事件只带业务码与可读 message，不带 DeepSeek 原始报错全文
                ctx.errorCode = node.path("code").asText("llm_upstream_error");
                ctx.errorMessage = node.path("message").asText(null);
            }
            default -> { /* meta 等事件：仅透传 */ }
        }
    }

    private static String trimField(String value) {
        return value.startsWith(" ") ? value.substring(1) : value;
    }

    private static Integer intOrNull(JsonNode node) {
        return node == null || !node.canConvertToInt() ? null : node.asInt();
    }

    // ------------------------------------------------------------------ 错误体映射（流前）

    /** 上游非 200：读最多 4KB 错误体，转发 {code,message} 与状态（§2.5 步骤 4）。 */
    private BizException mapUpstreamError(HttpResponse<InputStream> upstream) {
        int status = upstream.statusCode();
        String code = ErrorCode.UPSTREAM_ERROR.wireCode;
        String message = ErrorCode.UPSTREAM_ERROR.defaultMessage;
        try (InputStream in = upstream.body()) {
            byte[] bytes = in.readNBytes(4096);
            if (bytes.length > 0) {
                JsonNode node = objectMapper.readTree(bytes);
                if (node.hasNonNull("code")) {
                    code = node.get("code").asText();
                }
                if (node.hasNonNull("message")) {
                    message = node.get("message").asText();
                }
            }
        } catch (Exception ignore) {
            // 错误体不可解析 → 用默认 502 语义
        }
        log.warn("upstream non-200 status={} code={}", status, code);
        return new BizException(status, code, message);
    }

    // ------------------------------------------------------------------ 落库与 saved

    /** 写 query_history；失败不抛——saved 注入 historyId:null + ERROR 日志（§2.5 步骤 6）。 */
    private Long persistHistory(ChatSession session, StreamContext ctx, int status, long latencyMs) {
        try {
            QueryHistory h = new QueryHistory();
            h.setUserId(session.userId());
            h.setThreadId(session.threadId());
            h.setQuestion(session.question());
            String answer = ctx.answer.toString();
            h.setAnswerText(answer.isEmpty() ? null : answer);
            h.setGeneratedSql(ctx.generatedSql);
            h.setStatus(status);
            h.setErrorCode(ctx.errorCode);
            h.setLatencyMs((int) Math.min(latencyMs, Integer.MAX_VALUE));
            h.setTokenIn(ctx.tokenIn);
            h.setTokenOut(ctx.tokenOut);
            historyMapper.insert(h); // IdType.AUTO 回填 h.id
            return h.getId();
        } catch (Exception e) {
            // 错误日志只含业务字段（无 key）；异常里的 SQL 参数也不含 key（key 从不落库）
            log.error("query_history 落库失败 userId={} threadId={}（saved 事件将携带 historyId=null）",
                    session.userId(), session.threadId(), e);
            return null;
        }
    }

    /** 成功时向流内注入 saved 事件（前端反馈按钮用，§3.2）；客户端已断开则静默放弃。 */
    private void writeSaved(SseSink sink, Long historyId) {
        try {
            sink.writeLine("event: saved");
            sink.writeLine("data: {\"historyId\":" + (historyId == null ? "null" : historyId.toString()) + "}");
            sink.writeLine("");
            sink.flush();
        } catch (IOException e) {
            log.debug("saved 事件写出失败（客户端已断开）historyId={}", historyId);
        }
    }

    /** W5 §3.4：pending 轮结束时向流内注入 pending_saved 事件（实现思路复用 writeSaved）。
     *  前端把 historyId 绑进审批卡，批准/拒绝时随 /api/chat/approval 带回做原地 UPDATE。 */
    private void writePendingSaved(SseSink sink, Long historyId) {
        try {
            sink.writeLine("event: pending_saved");
            sink.writeLine("data: {\"historyId\":" + (historyId == null ? "null" : historyId.toString()) + "}");
            sink.writeLine("");
            sink.flush();
        } catch (IOException e) {
            log.debug("pending_saved 事件写出失败（客户端已断开）historyId={}", historyId);
        }
    }

    /**
     * W5 §3.4：审批流收场的 UPDATE（§3.6）——原行（openApproval 已查并随 session 携带）
     * 累加 latency/tokens 后写回；返回最终 status 供日志。
     *
     * status 语义：恢复轮又停在新 interrupt（链式审批，§8-⑰）→ 保持 4；恢复流 error 事件 →
     * 2（沿用既有失败语义）；客户端断连 → 3（§3.10）；reject → 3 + error_code=approval_rejected
     * （§7 T2 行：用户中断族）；approve → 1。
     */
    private int updateApprovalHistory(StreamContext ctx, QueryHistory original, String decision,
                                      boolean clientAborted, long wallLatencyMs) {
        int status;
        if (ctx.pending) {
            status = QueryHistory.STATUS_PENDING; // 链式审批：本轮已耗的部分照常累加，行保持待审批
        } else if (ctx.errorCode != null) {
            status = QueryHistory.STATUS_FAILED;
            original.setErrorCode(ctx.errorCode); // 恢复流中途出错（余额不足等）→ 沿用既有失败语义
        } else if (clientAborted) {
            status = QueryHistory.STATUS_ABORTED;
        } else if ("reject".equals(decision)) {
            status = QueryHistory.STATUS_ABORTED;
            original.setErrorCode(ERROR_APPROVAL_REJECTED);
        } else {
            status = QueryHistory.STATUS_SUCCESS;
        }
        original.setStatus(status);
        // answer_text 取两段之和：停车轮前言（原行已有）+ 恢复轮作答，与前端气泡续写一致
        String merged = (original.getAnswerText() == null ? "" : original.getAnswerText()) + ctx.answer;
        original.setAnswerText(merged.isEmpty() ? null : merged);
        long resumeLatency = ctx.latencyMs != null ? ctx.latencyMs : wallLatencyMs;
        int baseLatency = original.getLatencyMs() == null ? 0 : original.getLatencyMs();
        original.setLatencyMs((int) Math.min(baseLatency + resumeLatency, (long) Integer.MAX_VALUE));
        if (ctx.generatedSql != null && !ctx.generatedSql.isBlank()) {
            original.setGeneratedSql(ctx.generatedSql); // resume 轮一般不再产生新 SQL；兜底更新
        }
        original.setTokenIn(sumTokens(original.getTokenIn(), ctx.tokenIn));
        original.setTokenOut(sumTokens(original.getTokenOut(), ctx.tokenOut));
        historyMapper.updateById(original); // MP 默认策略：null 字段跳过（createdAt 等原值不动）
        return status;
    }

    /** 两段 token 之和：任一存在才写和（两段都缺保持 null——不过度承诺计费口径）。 */
    private static Integer sumTokens(Integer a, Integer b) {
        if (a == null && b == null) {
            return null;
        }
        return (a == null ? 0 : a) + (b == null ? 0 : b);
    }

    // ------------------------------------------------------------------ 校验与小工具

    private static void validateThreadId(String threadId) {
        if (threadId == null || threadId.isBlank()) {
            throw new BizException(ErrorCode.BAD_REQUEST, "threadId 不能为空");
        }
        try {
            UUID.fromString(threadId); // §2.4：前端生成的会话 uuid
        } catch (IllegalArgumentException e) {
            throw new BizException(ErrorCode.BAD_REQUEST, "threadId 必须是合法 uuid");
        }
    }

    private static void closeQuietly(HttpResponse<InputStream> response) {
        if (response != null) {
            try {
                response.body().close();
            } catch (IOException ignore) {
                // 静默关闭
            }
        }
    }

    /** 一次流的旁路解析状态（请求生命周期内存在，不含任何明文 key）。 */
    static final class StreamContext {
        String currentEvent;
        final StringBuilder answer = new StringBuilder();
        String lastTool;
        String generatedSql;
        String errorCode;
        String errorMessage;
        Integer tokenIn;
        Integer tokenOut;
        Integer latencyMs;
        /** W5 §3.6：收到 pending_approval 事件（本轮停在 interrupt 等审批）→ status=4 落库。 */
        boolean pending;
    }
}
