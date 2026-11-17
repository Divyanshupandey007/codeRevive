package com.example.crud.web;

import com.example.crud.model.User;
import com.example.crud.service.UserService;
import javax.validation.Valid;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class UserController {
    private final UserService userService;

    public UserController(UserService userService) {
        this.userService = userService;
    }

    @GetMapping("/users/{id}")
    public User findUser(@PathVariable Long id) {
        return userService.getUser(id);
    }

    @PostMapping("/users")
    public User createUser(@Valid @RequestBody User user) {
        return user;
    }
}
