package com.example.web;

import javax.validation.constraints.NotEmpty;
import org.springframework.stereotype.Controller;
import org.springframework.ui.Model;
import org.springframework.web.bind.annotation.GetMapping;
import org.thymeleaf.spring5.view.ThymeleafViewResolver;

@Controller
public class HomeController {
    @NotEmpty
    private String welcomeTitle = "Welcome";

    public String renderHome(Model model, ThymeleafViewResolver resolver) {
        model.addAttribute("title", welcomeTitle);
        return "layout";
    }
}
