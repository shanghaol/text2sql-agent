package com.shanghl.text2sql.llm;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.http.HttpClient;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.concurrent.atomic.AtomicInteger;

import com.shanghl.text2sql.config.AppProperties;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * DeepSeek /models 校验客户端单测：本地 JDK HttpServer 桩模拟 DeepSeek 的各种返回码
 * （§1.2 错误文案表的映射逻辑），不起 Spring、不出外网。
 */
class DeepSeekVerifyClientTest {

    private HttpServer server;
    private final AtomicInteger statusToReturn = new AtomicInteger(200);
    private final AtomicInteger delayMillis = new AtomicInteger(0);
    private final AtomicInteger hitCount = new AtomicInteger(0);
    private final java.util.concurrent.atomic.AtomicReference<String> capturedAuth =
            new java.util.concurrent.atomic.AtomicReference<>();

    private DeepSeekVerifyClient client;
    private AppProperties props;

    @BeforeEach
    void setUp() throws IOException {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/models", exchange -> {
            hitCount.incrementAndGet();
            capturedAuth.set(exchange.getRequestHeaders().getFirst("Authorization"));
            int delay = delayMillis.get();
            if (delay > 0) {
                try {
                    Thread.sleep(delay);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
            }
            int status = statusToReturn.get();
            byte[] body = "{\"object\":\"list\",\"data\":[]}".getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().add("Content-Type", "application/json");
            exchange.sendResponseHeaders(status, body.length);
            try (OutputStream out = exchange.getResponseBody()) {
                out.write(body);
            }
        });
        server.start();

        props = new AppProperties();
        props.getDeepseek().setBaseUrl("http://127.0.0.1:" + server.getAddress().getPort());
        HttpClient httpClient = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();
        client = new DeepSeekVerifyClient(httpClient, props);
    }

    @AfterEach
    void tearDown() {
        server.stop(0);
    }

    @Test
    @DisplayName("200 → VALID（key 存在且有效）")
    void validKey() {
        statusToReturn.set(200);
        assertThat(client.verify("sk-fake-but-format-ok-000")).isEqualTo(DeepSeekVerifyClient.VerifyOutcome.VALID);
    }

    @Test
    @DisplayName("401 → INVALID（key 无效或已被删除）")
    void invalidKey() {
        statusToReturn.set(401);
        assertThat(client.verify("sk-invalid")).isEqualTo(DeepSeekVerifyClient.VerifyOutcome.INVALID);
    }

    @Test
    @DisplayName("429 → RATE_LIMITED（DeepSeek 侧限流）")
    void rateLimited() {
        statusToReturn.set(429);
        assertThat(client.verify("sk-any")).isEqualTo(DeepSeekVerifyClient.VerifyOutcome.RATE_LIMITED);
    }

    @Test
    @DisplayName("5xx → UNREACHABLE（校验服务暂时不可用）")
    void serverErrorUnreachable() {
        statusToReturn.set(503);
        assertThat(client.verify("sk-any")).isEqualTo(DeepSeekVerifyClient.VerifyOutcome.UNREACHABLE);
    }

    @Test
    @DisplayName("响应超过校验超时（5s 纪律的缩小版）→ UNREACHABLE")
    void timeoutUnreachable() {
        props.getDeepseek().setVerifyTimeout(Duration.ofMillis(150)); // 缩小超时便于测试
        delayMillis.set(1000);
        assertThat(client.verify("sk-slow")).isEqualTo(DeepSeekVerifyClient.VerifyOutcome.UNREACHABLE);
    }

    @Test
    @DisplayName("Authorization 头形如 Bearer <key>（key 只走头，绝不进 URL——§1.5 规则 3）")
    void sendsBearerHeader() {
        statusToReturn.set(200);
        client.verify("sk-abc");
        assertThat(hitCount.get()).isEqualTo(1);
        assertThat(capturedAuth.get()).isEqualTo("Bearer sk-abc");
    }
}
