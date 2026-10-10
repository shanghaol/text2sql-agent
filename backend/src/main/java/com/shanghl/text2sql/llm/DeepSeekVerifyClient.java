package com.shanghl.text2sql.llm;

import java.io.IOException;
import java.net.ConnectException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.net.http.HttpTimeoutException;
import java.time.Duration;

import com.shanghl.text2sql.config.AppProperties;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

/**
 * DeepSeek key 有效性校验客户端（设计 §1.2 选型）：
 * **GET /models（Bearer 头带 key）**，被否"1-token completion"——
 * /models 零 token、免费、幂等，"重新校验"按钮和限流重试都可以随便触发；
 * 1-token 方案每次花钱且带副作用；余额缺口由对话时错误映射兜住（DeepSeek 402，§1.6）。
 *
 * 超时 5s（§1.2：超时/网络不通 → 502 verify_unreachable）。
 * 日志纪律（§1.5 规则 4/6）：只记异常类名与状态码，不打请求头（里面有明文 key）、不打 URL 查询串。
 */
@Component
public class DeepSeekVerifyClient {

    public enum VerifyOutcome {
        VALID,          // 200：key 存在且有效
        INVALID,        // 401/403：key 无效或已被删除
        RATE_LIMITED,   // 429：DeepSeek 侧限流
        UNREACHABLE     // 超时/网络不通/5xx：校验服务暂时不可用
    }

    private static final Logger log = LoggerFactory.getLogger(DeepSeekVerifyClient.class);

    private final HttpClient httpClient;
    private final AppProperties props;

    public DeepSeekVerifyClient(HttpClient httpClient, AppProperties props) {
        this.httpClient = httpClient;
        this.props = props;
    }

    public VerifyOutcome verify(String apiKey) {
        HttpRequest request = HttpRequest.newBuilder(URI.create(props.getDeepseek().getBaseUrl() + "/models"))
                .timeout(props.getDeepseek().getVerifyTimeout()) // 5s 同步校验超时
                .header("Authorization", "Bearer " + apiKey)
                .header("Accept", "application/json")
                .GET()
                .build();
        try {
            // discarding：不读响应体——/models 的响应体没有我们需要的信息，也避免任何落日志的可能
            HttpResponse<Void> response =
                    httpClient.send(request, HttpResponse.BodyHandlers.discarding());
            int status = response.statusCode();
            log.info("deepseek /models verify status={}", status);
            return switch (status) {
                case 200 -> VerifyOutcome.VALID;
                case 401, 403 -> VerifyOutcome.INVALID;
                case 429 -> VerifyOutcome.RATE_LIMITED;
                default -> VerifyOutcome.UNREACHABLE; // 5xx 等视为校验服务不可用
            };
        } catch (HttpTimeoutException e) {
            log.warn("deepseek /models verify timeout ({}ms)", props.getDeepseek().getVerifyTimeout().toMillis());
            return VerifyOutcome.UNREACHABLE;
        } catch (ConnectException e) {
            log.warn("deepseek /models verify connect failed: {}", e.getClass().getSimpleName());
            return VerifyOutcome.UNREACHABLE;
        } catch (IOException e) {
            log.warn("deepseek /models verify io error: {}", e.getClass().getSimpleName());
            return VerifyOutcome.UNREACHABLE;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return VerifyOutcome.UNREACHABLE;
        }
    }
}
