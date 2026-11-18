package com.example.crud.service;

import com.example.crud.model.User;
import java.sql.Connection;
import java.sql.SQLException;
import javax.sql.DataSource;
import org.springframework.stereotype.Service;

@Service
public class UserService {
    private final DataSource dataSource;

    public UserService(DataSource dataSource) {
        this.dataSource = dataSource;
    }

    public boolean checkConnection() throws SQLException {
        try (Connection conn = dataSource.getConnection()) {
            return conn != null;
        }
    }

    public User getUser(Long id) {
        User u = new User();
        u.setName("Alice");
        u.setEmail("alice@example.com");
        return u;
    }
}
