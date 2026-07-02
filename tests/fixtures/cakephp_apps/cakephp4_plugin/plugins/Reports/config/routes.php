<?php
$routes->plugin('Reports', function ($builder) {
    $builder->connect('/reports', ['controller' => 'Reports', 'action' => 'index']);
});
