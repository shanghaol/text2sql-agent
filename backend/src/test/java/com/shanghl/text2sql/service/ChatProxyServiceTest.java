package com.shanghl.text2sql.service;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.http.HttpClient;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicReference;

import javax.crypto.spec.SecretKeySpec;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.mockito.Mockito;

import com.baomidou.mybatisplus.core.MybatisConfiguration;
import com.baomidou.mybatisplus.core.metadata.TableInfoHelper;
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

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyBoolean;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.doReturn;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;

/**
 * ChatProxyService 逻辑单测（mock 上游 = 本地 JDK HttpServer，mock 落库 = Mockito）：
 * 成功透传+saved 注入、流前错误转发、key_not_set、error 事件、客户端中断 status=3、
 * 限流与并发闸 503、写库失败注入 historyId:null。不起 Spring、不依赖 MySQL。
 */
class ChatProxyServiceTest {

    private static final String PLAINTEXT_KEY = "sk-unit-test-key-0123456789abcdef";
    private static final String TEST_THREAD_ID = "550e8400-e29b-41d4-a716-446655440000";

    private HttpServer upstream;
    private ApiKeyMapper apiKeyMapper;
    private QueryHistoryMapper historyMapper;
    private UserMapper userMapper;
    private ChatProxyService service;
    private ConcurrencyGate gate;
    private AppProperties props;

    private final AtomicReference<byte[]> upstreamRequestBody = new AtomicReference<>();
    private final AtomicReference<String> upstreamKeyHeader = new AtomicReference<>();
    private final AtomicReference<String> upstreamUserIdHeader = new AtomicReference<>();
    private final AtomicReference<String> upstreamTokenHeader = new AtomicReference<>();
    private final AtomicReference<String> upstreamHitlHeader = new AtomicReference<>();

    @BeforeEach
    void setUp() throws IOException {
        // MP lambda 包装器需要 TableInfo 缓存（仅测试环境手动注册，主代码无感知）
        TableInfoHelper.initTableInfo(new org.apache.ibatis.builder.MapperBuilderAssistant(
                new MybatisConfiguration(), ""), ApiKey.class);
        TableInfoHelper.initTableInfo(new org.apache.ibatis.builder.MapperBuilderAssistant(
                new MybatisConfiguration(), ""), QueryHistory.class);
        TableInfoHelper.initTableInfo(new org.apache.ibatis.builder.MapperBuilderAssistant(
                new MybatisConfiguration(), ""), User.class);

        upstream = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        upstream.createContext("/agent/chat", this::defaultHandler);
        upstream.start();

        props = new AppProperties();
        props.getPython().setBaseUrl("http://127.0.0.1:" + upstream.getAddress().getPort());

        KeyCipherService cipher = new KeyCipherService(new SecretKeySpec(new byte[32], "AES"));
        ApiKey row = new ApiKey();
        row.setUserId(42L);
        KeyCipherService.SealedKey sealed = cipher.encrypt(PLAINTEXT_KEY);
        row.setKeyIv(sealed.iv());
        row.setKeyCiphertext(sealed.ciphertext());
        row.setKeyFingerprint(KeyCipherService.fingerprint(PLAINTEXT_KEY));

        apiKeyMapper = Mockito.mock(ApiKeyMapper.class);
        doReturn(row).when(apiKeyMapper).selectOne(any());
        doReturn(row).when(apiKeyMapper).selectOne(any(), anyBoolean());

        historyMapper = Mockito.mock(QueryHistoryMapper.class);
        doAnswer(inv -> {
            QueryHistory h = inv.getArgument(0);
            h.setId(123L);
            return 1;
        }).when(historyMapper).insert(any(QueryHistory.class));

        userMapper = Mockito.mock(UserMapper.class);
        doReturn(null).when(userMapper).selectById(Mockito.anyLong()); // 默认无设置行 → HITL 关

        gate = new ConcurrencyGate(4);
        HttpClient httpClient = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();
        service = new ChatProxyService(apiKeyMapper, historyMapper, userMapper, cipher, new RateLimiter(),
                gate, httpClient, new ObjectMapper(), props);
    }

    /** 默认上游：完整 SSE 事件序列（meta/delta/tool/done），并捕获 Java 发来的头三件与请求体。 */
    private void defaultHandler(HttpExchange exchange) throws IOException {
        upstreamRequestBody.set(exchange.getRequestBody().readAllBytes());
        upstreamKeyHeader.set(exchange.getRequestHeaders().getFirst("X-User-LLM-Key"));
        upstreamUserIdHeader.set(exchange.getRequestHeaders().getFirst("X-User-Id"));
        upstreamTokenHeader.set(exchange.getRequestHeaders().getFirst("X-Internal-Token"));
        upstreamHitlHeader.set(exchange.getRequestHeaders().getFirst("X-User-HITL"));
        byte[] body = String.join("\n",
                "event: meta",
                "data: {\"threadKey\":\"u42:t\",\"model\":\"deepseek-flash\"}",
                "",
                "event: delta",
                "data: {\"text\":\"你好\"}",
                "",
                "event: tool",
                "data: {\"name\":\"execute_readonly_sql\",\"sql\":\"SELECT 1\"}",
                "",
                "event: delta",
                "data: {\"text\":\"，结果为 1\"}",
                "",
                "event: done",
                "data: {\"tokens\":{\"input\":100,\"output\":10},\"latencyMs\":1234}",
                "",
                ": keep-alive", // 注释行也应原样透传
                "").getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().add("Content-Type", "text/event-stream");
        exchange.sendResponseHeaders(200, 0); // 0 = 分块/未知长度流式
        try (OutputStream out = exchange.getResponseBody()) {
            out.write(body);
            out.flush();
        }
    }

    private void upstreamRespond(int status, String contentType, byte[] body) {
        removeContextIfPresent("/agent/chat"); // JDK HttpServer 不允许同路径重复注册，先移除默认处理器
        upstream.createContext("/agent/chat", exchange -> {
            upstreamRequestBody.set(exchange.getRequestBody().readAllBytes());
            upstreamKeyHeader.set(exchange.getRequestHeaders().getFirst("X-User-LLM-Key"));
            exchange.getResponseHeaders().add("Content-Type", contentType);
            exchange.sendResponseHeaders(status, body.length == 0 ? -1 : body.length);
            if (body.length > 0) {
                try (OutputStream out = exchange.getResponseBody()) {
                    out.write(body);
                }
            }
        });
    }

    private void upstreamRespondStream(byte[] body) {
        removeContextIfPresent("/agent/chat");
        upstream.createContext("/agent/chat", exchange -> {
            exchange.getResponseHeaders().add("Content-Type", "text/event-stream");
            exchange.sendResponseHeaders(200, 0);
            try (OutputStream out = exchange.getResponseBody()) {
                out.write(body);
                out.flush();
            }
        });
    }

    @AfterEach
    void tearDown() {
        upstream.stop(0);
    }

    // ------------------------------------------------------------------ 用例

    @Test
    @DisplayName("成功路径：逐行透传 + 旁路解析 + status=1 落库 + saved 注入 historyId")
    void successFlow() {
        ChatSession session = service.open(42L, "上个月的GMV是多少", UUID.randomUUID().toString());
        CapturingSink sink = new CapturingSink();
        service.streamAndPersist(session, sink);

        // 头三件与请求体正确传递（§1.5 链路 ③）
        assertThat(upstreamKeyHeader.get()).isEqualTo(PLAINTEXT_KEY);
        assertThat(upstreamUserIdHeader.get()).isEqualTo("42");
        assertThat(upstreamRequestBody.get()).asString(StandardCharsets.UTF_8).contains("question");

        // 逐行透传（含 meta/delta/tool/done 与注释行），末尾注入 saved
        assertThat(sink.lines).anyMatch(l -> l.equals("event: delta"));
        assertThat(sink.lines).anyMatch(l -> l.equals(": keep-alive"));
        assertThat(sink.lines.subList(sink.lines.size() - 3, sink.lines.size()))
                .containsExactly("event: saved", "data: {\"historyId\":123}", "");

        ArgumentCaptor<QueryHistory> captor = ArgumentCaptor.forClass(QueryHistory.class);
        verify(historyMapper).insert(captor.capture());
        QueryHistory saved = captor.getValue();
        assertThat(saved.getStatus()).isEqualTo(QueryHistory.STATUS_SUCCESS);
        assertThat(saved.getAnswerText()).isEqualTo("你好，结果为 1");
        assertThat(saved.getGeneratedSql()).isEqualTo("SELECT 1");
        assertThat(saved.getTokenIn()).isEqualTo(100);
        assertThat(saved.getTokenOut()).isEqualTo(10);
        assertThat(saved.getLatencyMs()).isEqualTo(1234);
        assertThat(gate.inFlight()).isEqualTo(0); // 放闸
    }

    @Test
    @DisplayName("流前错误：上游 403 forbidden → 原样转发状态与业务码，且放闸不落库")
    void upstreamNon200Forwarded() {
        upstreamRespond(403, "application/json",
                "{\"code\":\"forbidden\",\"message\":\"内部头缺失\"}".getBytes(StandardCharsets.UTF_8));
        assertThatThrownBy(() -> service.open(42L, "q", UUID.randomUUID().toString()))
                .isInstanceOf(BizException.class)
                .satisfies(e -> {
                    BizException biz = (BizException) e;
                    assertThat(biz.httpStatus()).isEqualTo(403);
                    assertThat(biz.code()).isEqualTo("forbidden");
                });
        assertThat(gate.inFlight()).isEqualTo(0);
        verify(historyMapper, never()).insert(any(QueryHistory.class));
    }

    @Test
    @DisplayName("无 key 行 → 403 key_not_set（前端弹设置引导）")
    void keyNotSet() {
        doReturn(null).when(apiKeyMapper).selectOne(any());
        doReturn(null).when(apiKeyMapper).selectOne(any(), anyBoolean());
        assertThatThrownBy(() -> service.open(42L, "q", UUID.randomUUID().toString()))
                .isInstanceOf(BizException.class)
                .satisfies(e -> assertThat(((BizException) e).code())
                        .isEqualTo("key_not_set"));
        assertThat(gate.inFlight()).isEqualTo(0);
    }

    @Test
    @DisplayName("threadId 非 uuid → 422 bad_request")
    void badThreadIdRejected() {
        assertThatThrownBy(() -> service.open(42L, "q", "not-a-uuid"))
                .isInstanceOf(BizException.class)
                .satisfies(e -> {
                    BizException biz = (BizException) e;
                    assertThat(biz.code()).isEqualTo("bad_request");
                    assertThat(biz.httpStatus()).isEqualTo(422);
                });
        assertThat(gate.inFlight()).isEqualTo(0);
    }

    @Test
    @DisplayName("流中 error 事件 → status=2 落库 error_code，saved 仍注入")
    void errorEventPersistsFailed() {
        upstreamRespondStream(String.join("\n",
                "event: delta",
                "data: {\"text\":\"部分输出\"}",
                "",
                "event: error",
                "data: {\"code\":\"llm_insufficient_balance\",\"message\":\"DeepSeek 余额不足\"}",
                "").getBytes(StandardCharsets.UTF_8));
        ChatSession session = service.open(42L, "q", UUID.randomUUID().toString());
        CapturingSink sink = new CapturingSink();
        service.streamAndPersist(session, sink);

        ArgumentCaptor<QueryHistory> captor = ArgumentCaptor.forClass(QueryHistory.class);
        verify(historyMapper).insert(captor.capture());
        QueryHistory saved = captor.getValue();
        assertThat(saved.getStatus()).isEqualTo(QueryHistory.STATUS_FAILED);
        assertThat(saved.getErrorCode()).isEqualTo("llm_insufficient_balance");
        assertThat(saved.getAnswerText()).isEqualTo("部分输出");
        assertThat(sink.lines.subList(sink.lines.size() - 3, sink.lines.size()))
                .containsExactly("event: saved", "data: {\"historyId\":123}", "");
    }

    @Test
    @DisplayName("客户端中断（写出抛 IOException）→ status=3 落库部分答案，放闸，不向调用方抛异常")
    void clientAbortPersistsAborted() {
        upstreamRespondStream(String.join("\n",
                "event: delta",
                "data: {\"text\":\"第一段\"}",
                "",
                "event: delta",
                "data: {\"text\":\"第二段\"}",
                "",
                "event: done",
                "data: {\"tokens\":{\"input\":1,\"output\":1},\"latencyMs\":5}",
                "").getBytes(StandardCharsets.UTF_8));
        ChatSession session = service.open(42L, "q", UUID.randomUUID().toString());
        // 断在第 4 行（第二个 delta 的 event: 行）——旁路解析先于写出，因此第 5 行（第二段 data）
        // 根本不会被读入：断言"已有多少 delta 存多少"
        AbortAfterLines sink = new AbortAfterLines(3);
        service.streamAndPersist(session, sink); // 不应抛异常

        ArgumentCaptor<QueryHistory> captor = ArgumentCaptor.forClass(QueryHistory.class);
        verify(historyMapper).insert(captor.capture());
        QueryHistory saved = captor.getValue();
        assertThat(saved.getStatus()).isEqualTo(QueryHistory.STATUS_ABORTED);
        assertThat(saved.getAnswerText()).isEqualTo("第一段"); // 只存已收到的
        assertThat(gate.inFlight()).isEqualTo(0);
    }

    @Test
    @DisplayName("写库失败 → saved 注入 historyId:null（不弄断已收完的回答）")
    void historyInsertFailureInjectsNullHistoryId() {
        Mockito.reset(historyMapper);
        doAnswer(inv -> {
            throw new RuntimeException("db down");
        }).when(historyMapper).insert(any(QueryHistory.class));

        ChatSession session = service.open(42L, "q", UUID.randomUUID().toString());
        CapturingSink sink = new CapturingSink();
        service.streamAndPersist(session, sink);

        assertThat(sink.lines.subList(sink.lines.size() - 3, sink.lines.size()))
                .containsExactly("event: saved", "data: {\"historyId\":null}", "");
        assertThat(gate.inFlight()).isEqualTo(0);
    }

    @Test
    @DisplayName("对话限流：第 2 次/分钟被拒 rate_limited")
    void chatRateLimited() {
        props.getChat().setRateLimitPerMin(1);
        ChatSession session = service.open(42L, "q", UUID.randomUUID().toString());
        service.streamAndPersist(session, new CapturingSink());
        assertThatThrownBy(() -> service.open(42L, "q", UUID.randomUUID().toString()))
                .isInstanceOf(BizException.class)
                .satisfies(e -> assertThat(((BizException) e).code()).isEqualTo("rate_limited"));
    }

    @Test
    @DisplayName("并发闸：占满后第二个请求 503 concurrency_full；流结束后可再进")
    void concurrencyFull() {
        ConcurrencyGate tightGate = new ConcurrencyGate(1);
        KeyCipherService cipher = new KeyCipherService(new SecretKeySpec(new byte[32], "AES"));
        ChatProxyService tight = new ChatProxyService(apiKeyMapper, historyMapper, userMapper, cipher,
                new RateLimiter(), tightGate, HttpClient.newHttpClient(), new ObjectMapper(), props);

        ChatSession session = tight.open(42L, "q", UUID.randomUUID().toString());
        assertThatThrownBy(() -> tight.open(43L, "q", UUID.randomUUID().toString()))
                .isInstanceOf(BizException.class)
                .satisfies(e -> assertThat(((BizException) e).code()).isEqualTo("concurrency_full"));
        tight.streamAndPersist(session, new CapturingSink());
        assertThat(tightGate.inFlight()).isEqualTo(0);
        // 释放后可再次进入
        ChatSession again = tight.open(42L, "q", UUID.randomUUID().toString());
        tight.streamAndPersist(again, new CapturingSink());
    }

    @Test
    @DisplayName("上游连接拒绝（Python 未起）→ 502 upstream_unreachable（错误路径也要正确）")
    void upstreamDownMapsToUnreachable() throws IOException {
        HttpServer dead = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        dead.start();
        dead.stop(0); // 启动后立即停掉：端口已释放，连接将被拒绝
        int deadPort = dead.getAddress().getPort();
        props.getPython().setBaseUrl("http://127.0.0.1:" + deadPort);
        assertThatThrownBy(() -> service.open(42L, "q", UUID.randomUUID().toString()))
                .isInstanceOf(BizException.class)
                .satisfies(e -> {
                    BizException biz = (BizException) e;
                    assertThat(biz.httpStatus()).isEqualTo(502);
                    assertThat(biz.code()).isEqualTo("upstream_unreachable");
                });
        assertThat(gate.inFlight()).isEqualTo(0);
    }

    // ------------------------------------------------------------------ W5 HITL（§3.6）

    private void setUserHitl(Integer hitlEnabled) {
        User user = new User();
        user.setId(42L);
        user.setHitlEnabled(hitlEnabled);
        doReturn(user).when(userMapper).selectById(Mockito.anyLong());
    }

    private QueryHistory pendingRow(long historyId) {
        QueryHistory h = new QueryHistory();
        h.setId(historyId);
        h.setUserId(42L);
        h.setThreadId(TEST_THREAD_ID);
        h.setQuestion("上个月的GMV");
        h.setAnswerText("让我查一下");
        h.setGeneratedSql("SELECT SUM(amount) FROM payments");
        h.setStatus(QueryHistory.STATUS_PENDING);
        h.setLatencyMs(100);
        h.setTokenIn(10);
        h.setTokenOut(2);
        return h;
    }

    /** JDK HttpServer removeContext 对不存在的路径抛 IllegalArgumentException——幂等封装。 */
    private void removeContextIfPresent(String path) {
        try {
            upstream.removeContext(path);
        } catch (IllegalArgumentException ignore) {
            // 上下文已被本测试的前一步移除
        }
    }

    private void upstreamApprovalStream(byte[] body) {
        removeContextIfPresent("/agent/chat");
        removeContextIfPresent("/agent/chat/approval"); // 同一测试两段上游流时防重复注册
        upstream.createContext("/agent/chat/approval", exchange -> {
            upstreamRequestBody.set(exchange.getRequestBody().readAllBytes());
            upstreamKeyHeader.set(exchange.getRequestHeaders().getFirst("X-User-LLM-Key"));
            upstreamUserIdHeader.set(exchange.getRequestHeaders().getFirst("X-User-Id"));
            upstreamTokenHeader.set(exchange.getRequestHeaders().getFirst("X-Internal-Token"));
            upstreamHitlHeader.set(exchange.getRequestHeaders().getFirst("X-User-HITL"));
            exchange.getResponseHeaders().add("Content-Type", "text/event-stream");
            exchange.sendResponseHeaders(200, 0);
            try (OutputStream out = exchange.getResponseBody()) {
                out.write(body);
                out.flush();
            }
        });
    }

    private ApprovalRequest approvalReq(String decision) {
        return new ApprovalRequest(TEST_THREAD_ID, "approval-1", decision, 77L);
    }

    @Test
    @DisplayName("W5：pending_approval 事件 → status=4 落库 + pending_saved 注入（无 saved）")
    void pendingApprovalPersistsStatus4AndInjectsPendingSaved() {
        upstreamRespondStream(String.join("\n",
                "event: delta",
                "data: {\"text\":\"让我查一下\"}",
                "",
                "event: tool",
                "data: {\"name\":\"execute_readonly_sql\",\"sql\":\"SELECT SUM(amount) FROM payments\"}",
                "",
                "event: pending_approval",
                "data: {\"approvalId\":\"a1\",\"sql\":\"SELECT SUM(amount) FROM payments\",\"latencyMs\":800,\"tokens\":{\"input\":90,\"output\":12}}",
                "").getBytes(StandardCharsets.UTF_8));
        ChatSession session = service.open(42L, "q", UUID.randomUUID().toString());
        CapturingSink sink = new CapturingSink();
        service.streamAndPersist(session, sink);

        ArgumentCaptor<QueryHistory> captor = ArgumentCaptor.forClass(QueryHistory.class);
        verify(historyMapper).insert(captor.capture());
        QueryHistory saved = captor.getValue();
        assertThat(saved.getStatus()).isEqualTo(QueryHistory.STATUS_PENDING);
        assertThat(saved.getGeneratedSql()).isEqualTo("SELECT SUM(amount) FROM payments");
        assertThat(saved.getLatencyMs()).isEqualTo(800);          // additive 字段：停车前耗时
        assertThat(saved.getTokenIn()).isEqualTo(90);
        assertThat(saved.getTokenOut()).isEqualTo(12);
        assertThat(saved.getAnswerText()).isEqualTo("让我查一下");
        // 事件注入：pending_saved 携带 historyId；普通 saved 不出现
        assertThat(sink.lines.subList(sink.lines.size() - 3, sink.lines.size()))
                .containsExactly("event: pending_saved", "data: {\"historyId\":123}", "");
        assertThat(sink.lines).noneMatch("event: saved"::equals);
        assertThat(gate.inFlight()).isEqualTo(0); // 审批等待期间不占用并发额度（§3.4）
    }

    @Test
    @DisplayName("W5：用户开 HITL → open() 注入 X-User-HITL: 1；关 → 不注入")
    void hitlHeaderInjectedPerUserSetting() {
        setUserHitl(1);
        ChatSession session = service.open(42L, "q", UUID.randomUUID().toString());
        service.streamAndPersist(session, new CapturingSink());
        assertThat(upstreamHitlHeader.get()).isEqualTo("1");

        setUserHitl(0);
        ChatSession session2 = service.open(42L, "q", UUID.randomUUID().toString());
        service.streamAndPersist(session2, new CapturingSink());
        assertThat(upstreamHitlHeader.get()).isNull(); // 仅开启时注入（§3.6）
    }

    @Test
    @DisplayName("W5：批准 → 转发 /agent/chat/approval（头四件+HITL:1）→ 原行 UPDATE status=1 累加")
    void approvalApproveUpdatesHistoryRow() {
        QueryHistory original = pendingRow(77L);
        doReturn(original).when(historyMapper).selectById(77L);
        upstreamApprovalStream(String.join("\n",
                "event: delta",
                "data: {\"text\":\"，上月 GMV 为 1603 万元。\"}",
                "",
                "event: done",
                "data: {\"tokens\":{\"input\":50,\"output\":5},\"latencyMs\":200}",
                "").getBytes(StandardCharsets.UTF_8));
        ChatSession session = service.openApproval(42L, approvalReq("approve"));
        CapturingSink sink = new CapturingSink();
        service.streamAndPersistForApproval(session, sink, "approve");

        // 转发头四件 + 恒开 HITL
        assertThat(upstreamKeyHeader.get()).isEqualTo(PLAINTEXT_KEY);
        assertThat(upstreamUserIdHeader.get()).isEqualTo("42");
        assertThat(upstreamHitlHeader.get()).isEqualTo("1");
        assertThat(upstreamRequestBody.get()).asString(StandardCharsets.UTF_8)
                .contains("approvalId").contains("\"decision\":\"approve\"");
        // UPDATE（非 INSERT）：读原行累加——latency 100+200=300，token 10+50=60 / 2+5=7
        verify(historyMapper, never()).insert(any(QueryHistory.class));
        ArgumentCaptor<QueryHistory> captor = ArgumentCaptor.forClass(QueryHistory.class);
        verify(historyMapper).updateById(captor.capture());
        QueryHistory updated = captor.getValue();
        assertThat(updated.getId()).isEqualTo(77L);
        assertThat(updated.getStatus()).isEqualTo(QueryHistory.STATUS_SUCCESS);
        assertThat(updated.getAnswerText()).isEqualTo("让我查一下，上月 GMV 为 1603 万元。"); // 两段之和
        assertThat(updated.getLatencyMs()).isEqualTo(300);
        assertThat(updated.getTokenIn()).isEqualTo(60);
        assertThat(updated.getTokenOut()).isEqualTo(7);
        assertThat(updated.getErrorCode()).isNull();
        // saved 事件注入 historyId=77（前端续写气泡 + 反馈解锁）
        assertThat(sink.lines.subList(sink.lines.size() - 3, sink.lines.size()))
                .containsExactly("event: saved", "data: {\"historyId\":77}", "");
        assertThat(gate.inFlight()).isEqualTo(0);
    }

    @Test
    @DisplayName("W5：拒绝 → UPDATE status=3 + error_code=approval_rejected；resume 流 delta 照常透传")
    void approvalRejectPersistsRejected() {
        QueryHistory original = pendingRow(77L);
        doReturn(original).when(historyMapper).selectById(77L);
        upstreamApprovalStream(String.join("\n",
                "event: delta",
                "data: {\"text\":\"好的，请告诉我您希望如何调整？\"}",
                "",
                "event: done",
                "data: {\"tokens\":{\"input\":40,\"output\":8},\"latencyMs\":150}",
                "").getBytes(StandardCharsets.UTF_8));
        ChatSession session = service.openApproval(42L, approvalReq("reject"));
        CapturingSink sink = new CapturingSink();
        service.streamAndPersistForApproval(session, sink, "reject");

        ArgumentCaptor<QueryHistory> captor = ArgumentCaptor.forClass(QueryHistory.class);
        verify(historyMapper).updateById(captor.capture());
        QueryHistory updated = captor.getValue();
        assertThat(updated.getStatus()).isEqualTo(QueryHistory.STATUS_ABORTED);
        assertThat(updated.getErrorCode()).isEqualTo("approval_rejected");
        assertThat(updated.getAnswerText()).isEqualTo("让我查一下好的，请告诉我您希望如何调整？");
        assertThat(updated.getLatencyMs()).isEqualTo(250); // 100+150
        assertThat(updated.getTokenIn()).isEqualTo(50);    // 10+40
        assertThat(updated.getTokenOut()).isEqualTo(10);   // 2+8
        assertThat(sink.lines).anyMatch(l -> l.contains("请告诉我您希望如何调整")); // 透传照常
    }

    @Test
    @DisplayName("W5：审批流中途客户端断连 → status=3（§3.10）；恢复流出错事件 → status=2")
    void approvalAbortAndErrorSemantics() {
        QueryHistory original = pendingRow(77L);
        doReturn(original).when(historyMapper).selectById(77L);
        upstreamApprovalStream(String.join("\n",
                "event: delta",
                "data: {\"text\":\"第一段\"}",
                "",
                "event: delta",
                "data: {\"text\":\"第二段\"}",
                "").getBytes(StandardCharsets.UTF_8));
        ChatSession session = service.openApproval(42L, approvalReq("approve"));
        service.streamAndPersistForApproval(session, new AbortAfterLines(3), "approve");
        ArgumentCaptor<QueryHistory> captor = ArgumentCaptor.forClass(QueryHistory.class);
        verify(historyMapper).updateById(captor.capture());
        assertThat(captor.getValue().getStatus()).isEqualTo(QueryHistory.STATUS_ABORTED);
        assertThat(gate.inFlight()).isEqualTo(0);

        // 恢复流出错事件（余额不足）→ status=2 + 原样 error_code
        Mockito.clearInvocations(historyMapper);
        QueryHistory original2 = pendingRow(78L);
        doReturn(original2).when(historyMapper).selectById(78L);
        upstreamApprovalStream(String.join("\n",
                "event: error",
                "data: {\"code\":\"llm_insufficient_balance\",\"message\":\"DeepSeek 余额不足\"}",
                "").getBytes(StandardCharsets.UTF_8));
        ChatSession session2 = service.openApproval(42L,
                new ApprovalRequest(TEST_THREAD_ID, "approval-2", "approve", 78L));
        service.streamAndPersistForApproval(session2, new CapturingSink(), "approve");
        ArgumentCaptor<QueryHistory> captor2 = ArgumentCaptor.forClass(QueryHistory.class);
        verify(historyMapper).updateById(captor2.capture());
        assertThat(captor2.getValue().getStatus()).isEqualTo(QueryHistory.STATUS_FAILED);
        assertThat(captor2.getValue().getErrorCode()).isEqualTo("llm_insufficient_balance");
    }

    @Test
    @DisplayName("W5 §8-⑰ 链式审批：恢复轮又停在新 interrupt → 行保持 status=4 + pending_saved 续接")
    void approvalChainedInterruptKeepsPending() {
        QueryHistory original = pendingRow(79L);
        doReturn(original).when(historyMapper).selectById(79L);
        upstreamApprovalStream(String.join("\n",
                "event: delta",
                "data: {\"text\":\"第一条已执行\"}",
                "",
                "event: pending_approval",
                "data: {\"approvalId\":\"a2\",\"sql\":\"SELECT 2\",\"latencyMs\":600,\"tokens\":{\"input\":20,\"output\":4}}",
                "").getBytes(StandardCharsets.UTF_8));
        ChatSession session = service.openApproval(42L,
                new ApprovalRequest(TEST_THREAD_ID, "approval-1", "approve", 79L));
        CapturingSink sink = new CapturingSink();
        service.streamAndPersistForApproval(session, sink, "approve");

        ArgumentCaptor<QueryHistory> captor = ArgumentCaptor.forClass(QueryHistory.class);
        verify(historyMapper).updateById(captor.capture());
        QueryHistory updated = captor.getValue();
        assertThat(updated.getStatus()).isEqualTo(QueryHistory.STATUS_PENDING); // 保持待审批
        assertThat(updated.getErrorCode()).isNull();
        assertThat(updated.getLatencyMs()).isEqualTo(700); // 100+600 照常累加
        // pending_saved 续接下一张审批卡（historyId 不变），普通 saved 不出现
        assertThat(sink.lines.subList(sink.lines.size() - 3, sink.lines.size()))
                .containsExactly("event: pending_saved", "data: {\"historyId\":79}", "");
        assertThat(sink.lines).noneMatch("event: saved"::equals);
    }

    @Test
    @DisplayName("W5：openApproval 前置校验——decision 白名单/归属 404/状态幂等 409/限流并发复用")
    void openApprovalValidations() {
        // decision 白名单（@Valid 正则外的服务端兜底）
        assertThatThrownBy(() -> service.openApproval(42L, new ApprovalRequest(TEST_THREAD_ID, "a", "edit", 77L)))
                .isInstanceOf(BizException.class)
                .satisfies(e -> {
                    BizException biz = (BizException) e;
                    assertThat(biz.code()).isEqualTo("approval_bad_decision");
                    assertThat(biz.httpStatus()).isEqualTo(422);
                });
        assertThat(gate.inFlight()).isEqualTo(0);

        // 归属：非本人行一律 404（越权红线 §1.7，不泄露存在性）
        QueryHistory others = pendingRow(77L);
        others.setUserId(43L);
        doReturn(others).when(historyMapper).selectById(77L);
        assertThatThrownBy(() -> service.openApproval(42L, approvalReq("approve")))
                .isInstanceOf(BizException.class)
                .satisfies(e -> assertThat(((BizException) e).code()).isEqualTo("history_not_found"));

        // 幂等：行不在待审批态 → 409 approval_not_found（双击批准在 Java 侧的第二击兜底）
        QueryHistory done = pendingRow(77L);
        done.setStatus(QueryHistory.STATUS_SUCCESS);
        doReturn(done).when(historyMapper).selectById(77L);
        assertThatThrownBy(() -> service.openApproval(42L, approvalReq("approve")))
                .isInstanceOf(BizException.class)
                .satisfies(e -> {
                    BizException biz = (BizException) e;
                    assertThat(biz.code()).isEqualTo("approval_not_found");
                    assertThat(biz.httpStatus()).isEqualTo(409);
                });
        assertThat(gate.inFlight()).isEqualTo(0);
    }

    @Test
    @DisplayName("W7 §3.2：openApproval threadId 交叉校验——错 threadId 409；大小写不同放行（§8-9）")
    void openApprovalThreadIdCrossCheck() {
        QueryHistory original = pendingRow(77L); // 落库 threadId = TEST_THREAD_ID（小写十六进制）
        doReturn(original).when(historyMapper).selectById(77L);

        // 同用户把 A 会话的审批请求配上 B 会话的 threadId → 409 approval_not_found
        // （与"行不在待审批态"同码，不泄露该行是否存在于其他会话）
        assertThatThrownBy(() -> service.openApproval(42L,
                new ApprovalRequest(UUID.randomUUID().toString(), "approval-1", "approve", 77L)))
                .isInstanceOf(BizException.class)
                .satisfies(e -> {
                    BizException biz = (BizException) e;
                    assertThat(biz.code()).isEqualTo("approval_not_found");
                    assertThat(biz.httpStatus()).isEqualTo(409);
                });
        assertThat(gate.inFlight()).isEqualTo(0);

        // 大小写边界（§8-9）：大写 UUID threadId + 小写落库值 → 忽略大小写放行
        upstreamApprovalStream(String.join("\n",
                "event: done",
                "data: {\"tokens\":{\"input\":1,\"output\":1},\"latencyMs\":5}",
                "").getBytes(StandardCharsets.UTF_8));
        ChatSession session = service.openApproval(42L,
                new ApprovalRequest(TEST_THREAD_ID.toUpperCase(), "approval-1", "approve", 77L));
        assertThat(session).isNotNull();
        assertThat(session.pendingHistory()).isSameAs(original); // 原行随 session 带到落库段
        session.close(); // 只验前置链放行，不跑流（并发闸归还）
        assertThat(gate.inFlight()).isEqualTo(0);
    }

    // ------------------------------------------------------------------ 测试用 SseSink

    /** 内存捕获 sink。 */
    static final class CapturingSink implements SseSink {
        final List<String> lines = new ArrayList<>();

        @Override
        public void writeLine(String line) {
            lines.add(line);
        }

        @Override
        public void flush() {
        }
    }

    /** 写到第 N 行抛 IOException 的 sink（模拟客户端断连 → ClientAbortException 同路径）。 */
    static final class AbortAfterLines implements SseSink {
        private final CapturingSink delegate = new CapturingSink();
        private final int failAt;
        private int written = 0;

        AbortAfterLines(int failAt) {
            this.failAt = failAt;
        }

        @Override
        public void writeLine(String line) throws IOException {
            if (written++ >= failAt) {
                throw new IOException("client aborted (simulated ClientAbortException)");
            }
            delegate.writeLine(line);
        }

        @Override
        public void flush() {
        }
    }
}
