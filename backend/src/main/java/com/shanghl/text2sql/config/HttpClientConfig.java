package com.shanghl.text2sql.config;

import java.net.http.HttpClient;
import java.time.Duration;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * JDK 内置 java.net.http.HttpClient 单例（设计 §2.1：被否 WebClient/RestTemplate，
 * 流式消费用 BodyHandlers.ofInputStream() 正是 SSE 透传需要的原语；2C2G 内存友好）。
 * 连接超时 5s（§2.5 步骤 3）；读超时不设——SSE 流可能静默数十秒，靠容器层健康检查兜死连接，
 * 因此对话代理的请求上【绝不】设置 request timeout，只有 /models 校验在请求级单独设 5s。
 */
@Configuration
public class HttpClientConfig {

    @Bean
    public HttpClient httpClient() {
        return HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(5))
                .build();
    }
}
