package com.shanghl.text2sql.controller;

import java.io.IOException;
import java.nio.charset.StandardCharsets;

import com.shanghl.text2sql.service.SseSink;

import jakarta.servlet.ServletOutputStream;
import jakarta.servlet.WriteListener;
import jakarta.servlet.http.HttpServletResponse;

/**
 * Servlet 输出流实现（生产 SseSink）。
 * 刻意用 getOutputStream() + OutputStreamWriter 而非 getWriter()：
 * Tomcat 的 PrintWriter 会吞 IOException（只置错误标志），那样 ChatProxyService 就
 * 检测不到客户端断连（ClientAbortException 捕获依赖 IOException 冒泡，§2.5 步骤 6）。
 */
public final class ServletSseSink implements SseSink {

    private final ServletOutputStream raw;
    private final java.io.Writer writer;

    public ServletSseSink(HttpServletResponse response) throws IOException {
        this.raw = response.getOutputStream();
        this.writer = new java.io.OutputStreamWriter(raw, StandardCharsets.UTF_8);
    }

    @Override
    public void writeLine(String line) throws IOException {
        writer.write(line);
        writer.write('\n');
    }

    @Override
    public void flush() throws IOException {
        writer.flush();
        raw.flush(); // 双重冲刷：writer 缓冲 + servlet 输出缓冲都推到 socket
    }

    /** 供测试使用的最小桩（不依赖容器）。 */
    public static ServletOutputStream nullServletOutputStream() {
        return new ServletOutputStream() {
            @Override public boolean isReady() { return true; }
            @Override public void setWriteListener(WriteListener writeListener) { }
            @Override public void write(int b) { }
        };
    }
}
