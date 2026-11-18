package com.example.web;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;
import org.springframework.ui.ExtendedModelMap;

public class HomeControllerTest {
    @Test
    void rendersLayoutTemplate() {
        HomeController controller = new HomeController();
        String view = controller.renderHome(new ExtendedModelMap(), null);
        assertEquals("layout", view);
    }
}
