package com.example.legacy;

import com.example.legacy.model.Customer;
import com.example.legacy.service.CustomerService;
import org.springframework.context.ApplicationContext;
import org.springframework.context.support.ClassPathXmlApplicationContext;

public class LegacyApplication {

    public static void main(String[] args) {
        ApplicationContext context = new ClassPathXmlApplicationContext("applicationContext.xml");
        CustomerService customerService = (CustomerService) context.getBean("customerService");

        Customer customer = new Customer("Ada", "Lovelace", "ada@example.com");
        customerService.register(customer);

        System.out.println("Stored customers: " + customerService.list());
    }
}
