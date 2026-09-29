package com.example.legacy.dao;

import com.example.legacy.model.Customer;
import org.hibernate.Session;
import org.hibernate.SessionFactory;
import org.hibernate.Transaction;

import java.util.List;

public class HibernateCustomerDao implements CustomerDao {

    private SessionFactory sessionFactory;

    public void setSessionFactory(SessionFactory sessionFactory) {
        this.sessionFactory = sessionFactory;
    }

    @Override
    public void save(Customer customer) {
        Session session = sessionFactory.openSession();
        Transaction transaction = session.beginTransaction();
        try {
            session.save(customer);
            transaction.commit();
        } finally {
            session.close();
        }
    }

    @Override
    @SuppressWarnings("unchecked")
    public List<Customer> findAll() {
        Session session = sessionFactory.openSession();
        try {
            return session.createCriteria(Customer.class).list();
        } finally {
            session.close();
        }
    }
}
