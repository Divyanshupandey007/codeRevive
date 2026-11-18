package com.example.sec.filter;

import java.io.IOException;
import javax.crypto.Cipher;
import javax.servlet.FilterChain;
import javax.servlet.ServletException;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;
import org.springframework.web.filter.OncePerRequestFilter;

public class JwtAuthFilter extends OncePerRequestFilter {
    @Override
    protected void doFilterInternal(
            HttpServletRequest request,
            HttpServletResponse response,
            FilterChain filterChain)
            throws ServletException, IOException {
        String cipherAlgo = Cipher.class.getSimpleName();
        response.setHeader("X-Auth-Cipher", cipherAlgo);
        filterChain.doFilter(request, response);
    }
}
