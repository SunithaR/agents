package com.example.legacy.dao;

import com.example.legacy.model.Customer;

import java.util.List;

public interface CustomerDao {
    void save(Customer customer);
    List<Customer> findAll();
}
