package com.shanghl.text2sql;

import com.shanghl.text2sql.config.AppProperties;

import org.mybatis.spring.annotation.MapperScan;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.EnableConfigurationProperties;

/**
 * Text-to-SQL 分析助手 —— Java 网关（W4-T1）。
 * 职责边界（设计 §0.3）：Java 承担身份、密钥、审计、限流、传输；检索与推理在 Python 侧。
 */
@SpringBootApplication
@MapperScan("com.shanghl.text2sql.mapper")
@EnableConfigurationProperties(AppProperties.class)
public class Text2sqlApplication {

    public static void main(String[] args) {
        SpringApplication.run(Text2sqlApplication.class, args);
    }
}
