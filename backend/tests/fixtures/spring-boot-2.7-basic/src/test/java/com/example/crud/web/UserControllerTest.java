package com.example.crud.web;

import static org.junit.jupiter.api.Assertions.assertEquals;

import com.example.crud.model.User;
import com.example.crud.service.UserService;
import org.junit.jupiter.api.Test;

public class UserControllerTest {
    @Test
    void returnsUserById() {
        UserService service = new UserService(null);
        UserController controller = new UserController(service);
        User result = controller.findUser(1L);
        assertEquals("Alice", result.getName());
    }
}
