package com.example.legacy.service;

import com.example.legacy.dao.CustomerDao;
import com.example.legacy.model.Customer;

import java.util.List;

public class CustomerService {

    private CustomerDao customerDao;

    public void setCustomerDao(CustomerDao customerDao) {
        this.customerDao = customerDao;
    }

    public void register(Customer customer) {
        customerDao.save(customer);
    }

    public List<Customer> list() {
        return customerDao.findAll();
    }
}
