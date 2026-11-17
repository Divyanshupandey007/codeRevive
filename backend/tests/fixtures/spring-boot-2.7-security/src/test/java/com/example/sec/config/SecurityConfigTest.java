package com.example.sec.config;

import static org.junit.jupiter.api.Assertions.assertNotNull;

import org.junit.jupiter.api.Test;
import org.springframework.security.crypto.password.PasswordEncoder;

public class SecurityConfigTest {
    @Test
    void createsPasswordEncoder() {
        SecurityConfig config = new SecurityConfig();
        PasswordEncoder encoder = config.passwordEncoder();
        assertNotNull(encoder.encode("secret"));
    }
}
