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
import com.shanghl.text2sql.entity.ApiKey;
import com.shanghl.text2sql.entity.QueryHistory;
import com.shanghl.text2sql.mapper.ApiKeyMapper;
import com.shanghl.text2sql.mapper.QueryHistoryMapper;

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

    private HttpServer upstream;
    private ApiKeyMapper apiKeyMapper;
    private QueryHistoryMapper historyMapper;
    private ChatProxyService service;
    private ConcurrencyGate gate;
    private AppProperties props;

    private final AtomicReference<byte[]> upstreamRequestBody = new AtomicReference<>();
    private final AtomicReference<String> upstreamKeyHeader = new AtomicReference<>();
    private final AtomicReference<String> upstreamUserIdHeader = new AtomicReference<>();
    private final AtomicReference<String> upstreamTokenHeader = new AtomicReference<>();

    @BeforeEach
    void setUp() throws IOException {
        // MP lambda 包装器需要 TableInfo 缓存（仅测试环境手动注册，主代码无感知）
        TableInfoHelper.initTableInfo(new org.apache.ibatis.builder.MapperBuilderAssistant(
                new MybatisConfiguration(), ""), ApiKey.class);
        TableInfoHelper.initTableInfo(new org.apache.ibatis.builder.MapperBuilderAssistant(
                new MybatisConfiguration(), ""), QueryHistory.class);

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

        gate = new ConcurrencyGate(4);
        HttpClient httpClient = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();
        service = new ChatProxyService(apiKeyMapper, historyMapper, cipher, new RateLimiter(),
                gate, httpClient, new ObjectMapper(), props);
    }

    /** 默认上游：完整 SSE 事件序列（meta/delta/tool/done），并捕获 Java 发来的头三件与请求体。 */
    private void defaultHandler(HttpExchange exchange) throws IOException {
        upstreamRequestBody.set(exchange.getRequestBody().readAllBytes());
        upstreamKeyHeader.set(exchange.getRequestHeaders().getFirst("X-User-LLM-Key"));
        upstreamUserIdHeader.set(exchange.getRequestHeaders().getFirst("X-User-Id"));
        upstreamTokenHeader.set(exchange.getRequestHeaders().getFirst("X-Internal-Token"));
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
                        .isEqualTo(ErrorCode.KEY_NOT_SET.name()));
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
        AbortAfterLines sink = new AbortAfterLines(4); // 第 5 行（"第二段"的 data 行）处断开，第一段已收到
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
        ChatProxyService tight = new ChatProxyService(apiKeyMapper, historyMapper, cipher,
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
